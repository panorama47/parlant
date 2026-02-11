# Copyright 2026 Emcie Co Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dataclasses import dataclass
import json
from itertools import chain
import traceback
from typing import Optional, Sequence
from typing_extensions import override
from more_itertools import chunked

from parlant.core import async_utils
from parlant.core.common import DefaultBaseModel, JSONSerializable
from parlant.core.engines.alpha.guideline_matching.common import measure_response_analysis_batch
from parlant.core.engines.alpha.guideline_matching.generic.common import (
    GuidelineInternalRepresentation,
    internal_representation,
)
from parlant.core.engines.alpha.guideline_matching.generic.guideline_actionable_batch import (
    _make_event,
)
from parlant.core.engines.alpha.guideline_matching.guideline_match import (
    GuidelineMatch,
    AnalyzedGuideline,
)
from parlant.core.engines.alpha.guideline_matching.guideline_matcher import (
    ResponseAnalysisBatch,
    ResponseAnalysisBatchError,
    ResponseAnalysisBatchResult,
    ResponseAnalysisContext,
)
from parlant.core.engines.alpha.optimization_policy import OptimizationPolicy
from parlant.core.engines.alpha.prompt_builder import BuiltInSection, PromptBuilder
from parlant.core.guidelines import Guideline, GuidelineContent, GuidelineId
from parlant.core.loggers import Logger
from parlant.core.meter import Meter
from parlant.core.nlp.generation import SchematicGenerator
from parlant.core.nlp.generation_info import GenerationInfo, UsageInfo
from parlant.core.sessions import Event, EventSource
from parlant.core.shots import Shot, ShotCollection


class SegmentPreviouslyAppliedActionableRationale(DefaultBaseModel):
    action_segment: str
    action_applied_rationale: str


class GuidelinePreviouslyAppliedActionableDetectionSchema(DefaultBaseModel):
    guideline_id: str
    condition: Optional[str] = None
    action: str
    guideline_applied_rationale: Optional[list[SegmentPreviouslyAppliedActionableRationale]] = None
    guideline_applied_degree: Optional[str] = None
    is_missing_part_functional_or_behavioral_rationale: Optional[str] = None
    is_missing_part_functional_or_behavioral: Optional[str] = None
    guideline_applied: bool


class GenericResponseAnalysisSchema(DefaultBaseModel):
    checks: Sequence[GuidelinePreviouslyAppliedActionableDetectionSchema]


@dataclass
class GenericResponseAnalysisShot(Shot):
    interaction_events: Sequence[Event]
    guidelines: Sequence[GuidelineContent]
    expected_result: GenericResponseAnalysisSchema


class GenericResponseAnalysisBatch(ResponseAnalysisBatch):
    def __init__(
        self,
        logger: Logger,
        meter: Meter,
        optimization_policy: OptimizationPolicy,
        schematic_generator: SchematicGenerator[GenericResponseAnalysisSchema],
        context: ResponseAnalysisContext,
        guideline_matches: Sequence[GuidelineMatch],
    ) -> None:
        self._logger = logger
        self._meter = meter

        self._optimization_policy = optimization_policy
        self._schematic_generator = schematic_generator
        self._batch_size = 5

        self._context = context
        self._guideline_matches = guideline_matches

    @property
    @override
    def size(self) -> int:
        return len(self._guideline_matches)

    @override
    async def process(
        self,
    ) -> ResponseAnalysisBatchResult:
        all_guidelines = [m.guideline for m in self._guideline_matches]

        guideline_batches = list(chunked(all_guidelines, self._batch_size))

        batch_tasks = [
            self._process_batch(
                batch,
            )
            for batch in guideline_batches
        ]

        batch_results = await async_utils.safe_gather(*batch_tasks)

        all_analyzed_guidelines = list(
            chain.from_iterable(result.analyzed_guidelines for result in batch_results)
        )

        generation_info = (
            batch_results[-1].generation_info
            if batch_results
            else GenerationInfo(
                schema_name="",
                model="",
                duration=0.0,
                usage=UsageInfo(
                    input_tokens=0,
                    output_tokens=0,
                    extra={},
                ),
            )
        )

        return ResponseAnalysisBatchResult(
            analyzed_guidelines=all_analyzed_guidelines,
            generation_info=generation_info,
        )

    async def _process_batch(
        self,
        batch: Sequence[Guideline],
    ) -> ResponseAnalysisBatchResult:
        batch_guideline_ids = {g.id for g in batch}

        batch_guidelines = [
            m.guideline for m in self._guideline_matches if m.guideline.id in batch_guideline_ids
        ]

        guidelines = {str(i): g for i, g in enumerate(batch_guidelines, start=1)}

        async with measure_response_analysis_batch(self._meter, self):
            prompt = self._build_prompt(
                shots=await self.shots(),
                guidelines=guidelines,
            )

            generation_attempt_temperatures = (
                self._optimization_policy.get_response_analysis_batch_retry_temperatures(
                    hints={"type": self.__class__.__name__}
                )
            )

            last_generation_exception: Exception | None = None

            for generation_attempt in range(3):
                try:
                    inference = await self._schematic_generator.generate(
                        prompt=prompt,
                        hints={"temperature": generation_attempt_temperatures[generation_attempt]},
                    )

                    analyzed_guidelines: list[AnalyzedGuideline] = []

                    for check in inference.content.checks:
                        if check.guideline_applied:
                            self._logger.debug(f"Applied:\n{check.model_dump_json(indent=2)}")
                            analyzed_guidelines.append(
                                AnalyzedGuideline(
                                    guideline=guidelines[check.guideline_id],
                                    is_previously_applied=True,
                                )
                            )
                        else:
                            self._logger.debug(f"Not applied:\n{check.model_dump_json(indent=2)}")
                            analyzed_guidelines.append(
                                AnalyzedGuideline(
                                    guideline=guidelines[GuidelineId(check.guideline_id)],
                                    is_previously_applied=False,
                                )
                            )

                    return ResponseAnalysisBatchResult(
                        analyzed_guidelines=analyzed_guidelines,
                        generation_info=inference.info,
                    )

                except Exception as exc:
                    self._logger.warning(
                        f"Attempt {generation_attempt} failed: {traceback.format_exception(exc)}"
                    )

                    last_generation_exception = exc

            raise ResponseAnalysisBatchError() from last_generation_exception

    async def shots(self) -> Sequence[GenericResponseAnalysisShot]:
        return await shot_collection.list()

    def _format_shots(self, shots: Sequence[GenericResponseAnalysisShot]) -> str:
        return "\n".join(
            f"示例 #{i}：###\n{self._format_shot(shot)}" for i, shot in enumerate(shots, start=1)
        )

    def _format_shot(
        self,
        shot: GenericResponseAnalysisShot,
    ) -> str:
        def adapt_event(e: Event) -> JSONSerializable:
            source_map: dict[EventSource, str] = {
                EventSource.CUSTOMER: "user",
                EventSource.CUSTOMER_UI: "frontend_application",
                EventSource.HUMAN_AGENT: "human_service_agent",
                EventSource.HUMAN_AGENT_ON_BEHALF_OF_AI_AGENT: "ai_agent",
                EventSource.AI_AGENT: "ai_agent",
                EventSource.SYSTEM: "system-provided",
            }

            return {
                "event_kind": e.kind.value,
                "event_source": source_map[e.source],
                "data": e.data,
            }

        formatted_shot = ""
        if shot.interaction_events:
            formatted_shot += f"""
- **交互事件**：
{json.dumps([adapt_event(e) for e in shot.interaction_events], indent=2)}

"""
        if shot.guidelines:
            formatted_guidelines = "\n".join(
                f"{i}) 条件：{g.condition}，动作：{g.action}"
                for i, g in enumerate(shot.guidelines, start=1)
            )
            formatted_shot += f"""
- **指导原则**：
{formatted_guidelines}

"""

        formatted_shot += f"""
- **期望结果**：
```json
{json.dumps(shot.expected_result.model_dump(mode="json", exclude_unset=True), indent=2)}
```
"""

        return formatted_shot

    def _add_guideline_matches_section(
        self,
        guidelines: dict[str, Guideline],
        guideline_representations: dict[GuidelineId, GuidelineInternalRepresentation],
    ) -> str:
        guidelines_text = "\n".join(
            f"{i}) 条件：{guideline_representations[g.id].condition}。动作：{guideline_representations[g.id].action}"
            for i, g in guidelines.items()
        )

        return f"""
指导原则
---------------------
以下指导原则需评估是否已被应用。

指导原则：
###
{guidelines_text}
###
"""

    def _build_prompt(
        self,
        shots: Sequence[GenericResponseAnalysisShot],
        guidelines: dict[str, Guideline],
    ) -> PromptBuilder:
        guideline_representations = {g.id: internal_representation(g) for g in guidelines.values()}

        builder = PromptBuilder(on_build=lambda prompt: self._logger.trace(f"Prompt:\n{prompt}"))

        builder.add_agent_identity(self._context.agent)

        builder.add_section(
            name="guideline-previously-applied-general-instructions",
            template="""
总体说明
-----------------
本系统中，对话 AI 客服的行为由「指导原则」指导。客服在与用户（也称客户）交互时会使用这些指导原则。
每条指导原则由两部分组成：
-「条件」：用自然语言描述指导原则何时适用。我们根据对话任一状态检查该条件，以判断该指导原则是否应参与生成对用户的下一条回复。
-「动作」：当指导原则的「条件」在对话特定状态下成立时，客服应遵循的自然语言指令。此处描述仅针对客服，不针对用户。


任务说明
----------------
你的任务是评估每条指导原则指定的动作是否已被应用。你正在审查的指导原则尚未标记为已应用，需判断对话中客服的最新消息是否满足其动作，从而使该动作可视为已应用。

1. 动作评估中关注客服侧要求：
某些指导原则可能包含 依赖客户回复的要求。 For example, an action like "get the customer's card number" requires the agent to ask for this information, and the customer to provide it for full
completion. In such cases, you should evaluate only the agent’s part of the action. Since evaluation occurs after the agent’s message, the action is considered applied if the agent has done its part (e.g., asked for the information),
不论客户是否已回复。

2. 区分功能性与行为性动作
某些指导原则包含多个动作。若仅部分已履行，需判断缺失部分属功能性还是行为性。

-「功能性」动作直接有助于 to resolving the customer’s issue or progressing the task at hand. These actions are core to the outcome of the interaction. If omitted, they may leave the issue unresolved, cause confusion,
或使回复无效。
若功能性动作缺失，指导原则不应视为已应用。

-「行为性」动作与交互的语气、共情或礼貌相关。这类动作提升客户体验和信任，但对达成客户目标并非关键。
若行为性动作缺失但功能性需求已满足，可将指导原则视为已应用。

行为性动作示例：
- 表达共情或理解
- 致歉或表示遗憾
- 感谢客户
- 使用礼貌对话用语（如问候、结束语）
- 给予鼓励或安抚
- 使用精确或品牌偏好的措辞传达已表述内容

行为性动作在当下使用时最有效，无需事后补做。 其缺失不要求将指导原则标为未履行。
可参考判断：
“If the conversation were to continue, would the agent need to go back and perform that missing action?”
若答案为否，可能为行为性，指导原则可视为已履行。
若答案为是，可能为功能性，指导原则仍为未履行。

3. 无论条件如何均评估动作：
给定条件-动作指导原则，仅评估动作是否已执行——如同条件已满足。有时动作可能因其他原因执行——由另一指导原则的条件触发，或在交互中自发提供。但就评估而言，我们只检查动作是否发生，不论原因。因此，即便指导原则中的条件不是执行动作的原因，动作仍计为已履行。

""",
            props={},
        )
        builder.add_section(
            name="guideline-previously-applied-examples",
            template="""
指导原则应用评估示例：
-------------------
{formatted_shots}
""",
            props={
                "formatted_shots": self._format_shots(shots),
                "shots": shots,
            },
        )
        builder.add_context_variables(self._context.context_variables)
        builder.add_glossary(self._context.terms)
        builder.add_customer_identity(self._context.customer, self._context.session)
        builder.add_interaction_history(
            self._context.interaction_history,
            staged_events=self._context.staged_message_events,
        )
        builder.add_staged_tool_events(self._context.staged_tool_events)
        builder.add_section(
            name=BuiltInSection.GUIDELINE_DESCRIPTIONS,
            template=self._add_guideline_matches_section(guidelines, guideline_representations),
            props={},
        )

        builder.add_section(
            name="guideline-previously-applied-output-format",
            template="""
重要：列表中恰好有 {guidelines_len} 条指导原则需要你检查。

输出格式
-----------------
- 按说明填写下列列表中每条指导原则是否已被应用：
```json
{result_structure_text}
```
""",
            props={
                "result_structure_text": self._format_of_guideline_check_json_description(
                    guidelines=guidelines,
                    guideline_representations=guideline_representations,
                ),
                "guidelines_len": len(guidelines),
            },
        )
        return builder

    def _format_of_guideline_check_json_description(
        self,
        guidelines: dict[str, Guideline],
        guideline_representations: dict[GuidelineId, GuidelineInternalRepresentation],
    ) -> str:
        result_structure = [
            {
                "guideline_id": i,
                # "condition": g.content.condition,
                "action": guideline_representations[g.id].action,
                "guideline_applied_rationale": [
                    {
                        "action_segment": "<动作片段描述>",
                        "action_applied_rationale": "<说明该动作片段（不含条件）是否已被客服应用；为避免偏差，尽量使用与动作片段相同的措辞判断，并用大写突出片段与说明中的相同词语>",
                    }
                ],
                "guideline_applied_degree": "<str：根据动作（不含条件）是否及在何种程度上已执行，取 'no'、'partially' 或 'fully'>",
                "is_missing_part_functional_or_behavioral_rationale": "<str：仅当 guideline_applied 为 'partially' 时包含。简短说明缺失部分属功能性还是行为性。>",
                "is_missing_part_functional_or_behavioral": "<str：仅当 guideline_applied 为 'partially' 时包含。>",
                "guideline_applied": "<bool>",
            }
            for i, g in guidelines.items()
        ]
        result = {"checks": result_structure}
        return json.dumps(result, indent=4)


example_1_events = [
    _make_event("11", EventSource.CUSTOMER, "Can I purchase a subscription to your software?"),
    _make_event("23", EventSource.AI_AGENT, "Absolutely, I can assist you with that right now."),
    _make_event(
        "34", EventSource.CUSTOMER, "Cool, let's go with the subscription for the Pro plan."
    ),
    _make_event(
        "56",
        EventSource.AI_AGENT,
        "Your subscription has been successfully activated. Is there anything else I can help you with?",
    ),
    _make_event(
        "88",
        EventSource.CUSTOMER,
        "Will my son be able to see that I'm subscribed? Or is my data protected?",
    ),
    _make_event(
        "98",
        EventSource.AI_AGENT,
        "If your son is not a member of your same household account, he won't be able to see your subscription. Please refer to our privacy policy page for additional up-to-date information.",
    ),
]

example_1_guidelines = [
    GuidelineContent(
        condition="the customer initiates a purchase.",
        action="Open a new cart for the customer",
    ),
    GuidelineContent(
        condition="the customer asks about data security",
        action="Refer the customer to our privacy policy page",
    ),
]


example_1_expected = GenericResponseAnalysisSchema(
    checks=[
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="the customer initiates a purchase.",
            action="Open a new cart for the customer",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="OPEN a new cart for the customer",
                    action_applied_rationale="No cart was opened",
                )
            ],
            guideline_applied_degree="no",
            guideline_applied=False,
        ),
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="the customer asks about data security",
            action="Refer the customer to our privacy policy page",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="REFER the customer to our privacy policy page",
                    action_applied_rationale="The customer has been REFERRED to the privacy policy page.",
                )
            ],
            guideline_applied_degree="fully",
            guideline_applied=True,
        ),
    ]
)

example_2_events = [
    _make_event("11", EventSource.CUSTOMER, "I'm looking for a job, what do you have available?"),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "Hi there! we have plenty of opportunities for you, where are you located?",
    ),
    _make_event("34", EventSource.CUSTOMER, "I'm looking for anything around the bay area"),
    _make_event(
        "56",
        EventSource.AI_AGENT,
        "That's great. We have a number of positions available over there. What kind of role are you interested in?",
    ),
]

example_2_guidelines = [
    GuidelineContent(
        condition="the customer indicates that they are looking for a job.",
        action="ask the customer for their location and what kind of role they are looking for",
    ),
    GuidelineContent(
        condition="the customer asks about job openings.",
        action="emphasize that we have plenty of positions relevant to the customer, and over 10,000 openings overall",
    ),
]

example_2_expected = GenericResponseAnalysisSchema(
    checks=[
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="the customer indicates that they are looking for a job.",
            action="ask the customer for their location and what kind of role they are looking for",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="ASK the customer for their location",
                    action_applied_rationale="The agent ASKED for the customer's location earlier in the interaction.",
                ),
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="ASK the customer what kind of role they are looking for",
                    action_applied_rationale="The agent ASKED what kind of role they customer is interested in.",
                ),
            ],
            guideline_applied_degree="fully",
            guideline_applied=True,
        ),
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="the customer asks about job openings.",
            action="emphasize that we have plenty of positions relevant to the customer, and over 10,000 openings overall",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="EMPHASIZE we have plenty of relevant positions",
                    action_applied_rationale="The agent already has EMPHASIZED (i.e. clearly stressed) that we have open positions",
                ),
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="EMPHASIZE we have over 10,000 openings overall",
                    action_applied_rationale="The agent neglected to EMPHASIZE (i.e. clearly stressed) that we offer 10k openings overall.",
                ),
            ],
            guideline_applied_degree="partially",
            is_missing_part_functional_or_behavioral_rationale="overall intention that there are many open position was made clear so using the exact words is behavioral",
            is_missing_part_functional_or_behavioral="behavioral",
            guideline_applied=True,
        ),
    ]
)


example_3_events = [
    _make_event("11", EventSource.CUSTOMER, "I'm looking for a job, what do you have available?"),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "Hi there! we have plenty of opportunities for you, where are you located?",
    ),
]

example_3_guidelines = [
    GuidelineContent(
        condition="the customer indicates that they are looking for a job.",
        action="ask the customer for their location and what kind of role they are looking for",
    ),
]

example_3_expected = GenericResponseAnalysisSchema(
    checks=[
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="the customer indicates that they are looking for a job.",
            action="ask the customer for their location and what kind of role they are looking for",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="ASK the customer for their location",
                    action_applied_rationale="The agent ASKED for the customer's location earlier in the interaction.",
                ),
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="ASK the customer what kind of role they are looking for",
                    action_applied_rationale="The agent did not ASK what kind of role the customer is interested in.",
                ),
            ],
            guideline_applied_degree="partially",
            is_missing_part_functional_or_behavioral_rationale="Need to ask for the kind of role so can narrow the option and help the customer find the right job fit",
            is_missing_part_functional_or_behavioral="functional",
            guideline_applied=False,
        ),
    ]
)


example_4_events = [
    _make_event("11", EventSource.CUSTOMER, "My screen is frozen and nothing's responding."),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "No problem — I can help reset your password for you. Let me guide you through it.",
    ),
]

example_4_guidelines = [
    GuidelineContent(
        condition="the customer says they forgot their password",
        action="Offer to reset the password and guide them through the process",
    ),
]

example_4_expected = GenericResponseAnalysisSchema(
    checks=[
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="the customer says they forgot their password",
            action="Offer to reset the password.",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="OFFER to reset the password",
                    action_applied_rationale="The agent indeed OFFERED to reset the password.",
                ),
            ],
            guideline_applied_degree="fully",
            guideline_applied=True,
        ),
    ]
)


example_5_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "I've been waiting 40 minutes for my order and it still hasn’t arrived.",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "I'm really sorry about the delay. We’re checking with the delivery partner right now and will update you shortly.",
    ),
]

example_5_guidelines = [
    GuidelineContent(
        condition="there is a problem with the order",
        action="Acknowledge the issue and thank the user for their patience.",
    ),
]

example_5_expected = GenericResponseAnalysisSchema(
    checks=[
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="there is a problem with the order",
            action="Acknowledge the issue and thank the user for their patience.",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="ACKNOWLEDGE the issue",
                    action_applied_rationale="The agent ACKNOWLEDGED the issue by saying they are checking it",
                ),
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="THANK the user for their patience.",
                    action_applied_rationale="The agent didn't thank the customer for their patient",
                ),
            ],
            guideline_applied_degree="partially",
            is_missing_part_functional_or_behavioral_rationale="missing part is about tone and politeness, and doesn’t affect the quality of solving the issue."
            "There’s no need to return and thank the user later in order to complete the response.",
            is_missing_part_functional_or_behavioral="behavioral",
            guideline_applied=True,
        ),
    ]
)


example_6_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "I've been waiting 40 minutes for my order and it still hasn’t arrived.",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "I'm really sorry about the inconvenience. We’re checking with the delivery partner right now and will update you shortly. Any way, let me give you a refund of $20",
    ),
]

example_6_guidelines = [
    GuidelineContent(
        condition="The customer reports that a product arrived damaged",
        action="Offer a $20 refund on the purchase.",
    ),
]

example_6_expected = GenericResponseAnalysisSchema(
    checks=[
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="The customer reports that a product arrived damaged",
            action="Offer a $20 refund on the purchase.",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="OFFER a $20 refund on the purchase.",
                    action_applied_rationale="The agent OFFERED $20 refund for the delay, although not for damaged item.",
                ),
            ],
            guideline_applied_degree="fully",
            guideline_applied=True,
        ),
    ]
)

example_7_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "OK I don't need any other help.",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "Great I was happy to help you, bye bye!",
    ),
]

example_7_guidelines = [
    GuidelineContent(
        condition="The customer said they don't need any other help",
        action="Wish the customer a great day at the end of the interaction by saying goodbye.",
    ),
]

example_7_expected = GenericResponseAnalysisSchema(
    checks=[
        GuidelinePreviouslyAppliedActionableDetectionSchema(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            # condition="The customer said they don't need any other help",
            action="Wish the customer a great day at the end of the interaction.",
            guideline_applied_rationale=[
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="Wish the customer a great day",
                    action_applied_rationale="The agent didn't WISH a great day",
                ),
                SegmentPreviouslyAppliedActionableRationale(
                    action_segment="END of the interaction.",
                    action_applied_rationale="The agent END the interaction by saying goodbye.",
                ),
            ],
            guideline_applied_degree="partially",
            is_missing_part_functional_or_behavioral_rationale="missing part is about politeness, and doesn’t affect the quality of the interaction",
            is_missing_part_functional_or_behavioral="behavioral",
            guideline_applied=True,
        ),
    ]
)

_baseline_shots: Sequence[GenericResponseAnalysisShot] = [
    GenericResponseAnalysisShot(
        description="",
        interaction_events=example_1_events,
        guidelines=example_1_guidelines,
        expected_result=example_1_expected,
    ),
    GenericResponseAnalysisShot(
        description="",
        interaction_events=example_2_events,
        guidelines=example_2_guidelines,
        expected_result=example_2_expected,
    ),
    GenericResponseAnalysisShot(
        description="",
        interaction_events=example_3_events,
        guidelines=example_3_guidelines,
        expected_result=example_3_expected,
    ),
    GenericResponseAnalysisShot(
        description="",
        interaction_events=example_4_events,
        guidelines=example_4_guidelines,
        expected_result=example_4_expected,
    ),
    GenericResponseAnalysisShot(
        description="",
        interaction_events=example_5_events,
        guidelines=example_5_guidelines,
        expected_result=example_5_expected,
    ),
    GenericResponseAnalysisShot(
        description="",
        interaction_events=example_6_events,
        guidelines=example_6_guidelines,
        expected_result=example_6_expected,
    ),
    GenericResponseAnalysisShot(
        description="",
        interaction_events=example_7_events,
        guidelines=example_7_guidelines,
        expected_result=example_7_expected,
    ),
]

shot_collection = ShotCollection[GenericResponseAnalysisShot](_baseline_shots)
