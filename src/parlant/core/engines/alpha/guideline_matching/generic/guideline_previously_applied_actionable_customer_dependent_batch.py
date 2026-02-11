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
from datetime import datetime, timezone
import json
import math
import traceback
from typing import Optional, Sequence
from typing_extensions import override
from parlant.core.common import DefaultBaseModel, JSONSerializable
from parlant.core.engines.alpha.guideline_matching.common import measure_guideline_matching_batch
from parlant.core.engines.alpha.guideline_matching.generic.common import (
    GuidelineInternalRepresentation,
    internal_representation,
)
from parlant.core.engines.alpha.guideline_matching.guideline_match import (
    GuidelineMatch,
)
from parlant.core.engines.alpha.guideline_matching.guideline_matcher import (
    GuidelineMatchingBatch,
    GuidelineMatchingBatchResult,
    GuidelineMatchingBatchError,
    GuidelineMatchingStrategy,
)
from parlant.core.engines.alpha.guideline_matching.guideline_matching_context import (
    GuidelineMatchingContext,
)
from parlant.core.engines.alpha.optimization_policy import OptimizationPolicy
from parlant.core.engines.alpha.prompt_builder import BuiltInSection, PromptBuilder, SectionStatus
from parlant.core.entity_cq import EntityQueries
from parlant.core.guidelines import Guideline, GuidelineContent, GuidelineId
from parlant.core.journeys import Journey
from parlant.core.loggers import Logger
from parlant.core.meter import Meter
from parlant.core.nlp.generation import SchematicGenerator
from parlant.core.sessions import Event, EventId, EventKind, EventSource
from parlant.core.shots import Shot, ShotCollection


class GenericPreviouslyAppliedActionableCustomerDependentBatch(DefaultBaseModel):
    guideline_id: str
    condition: str
    action: str
    condition_still_met: bool
    customer_should_reply: Optional[bool] = None
    condition_met_again: Optional[bool] = None
    action_should_reapply: Optional[bool] = None
    action_wasnt_taken: Optional[bool] = None
    tldr: str
    should_apply: bool


class GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema(DefaultBaseModel):
    checks: Sequence[GenericPreviouslyAppliedActionableCustomerDependentBatch]


@dataclass
class GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot(Shot):
    interaction_events: Sequence[Event]
    guidelines: Sequence[GuidelineContent]
    expected_result: GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema


class GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingBatch(
    GuidelineMatchingBatch
):
    def __init__(
        self,
        logger: Logger,
        meter: Meter,
        optimization_policy: OptimizationPolicy,
        schematic_generator: SchematicGenerator[
            GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema
        ],
        guidelines: Sequence[Guideline],
        journeys: Sequence[Journey],
        context: GuidelineMatchingContext,
    ) -> None:
        self._logger = logger
        self._meter = meter

        self._optimization_policy = optimization_policy
        self._schematic_generator = schematic_generator
        self._guidelines = {str(i): g for i, g in enumerate(guidelines, start=1)}
        self._journeys = journeys
        self._context = context

    @property
    @override
    def size(self) -> int:
        return len(self._guidelines)

    @override
    async def process(self) -> GuidelineMatchingBatchResult:
        async with measure_guideline_matching_batch(self._meter, self):
            prompt = self._build_prompt(shots=await self.shots())

            try:
                generation_attempt_temperatures = (
                    self._optimization_policy.get_guideline_matching_batch_retry_temperatures(
                        hints={"type": self.__class__.__name__}
                    )
                )

                last_generation_exception: Exception | None = None

                for generation_attempt in range(3):
                    inference = await self._schematic_generator.generate(
                        prompt=prompt,
                        hints={"temperature": generation_attempt_temperatures[generation_attempt]},
                    )

                    if not inference.content.checks:
                        self._logger.warning(
                            "Completion:\nNo checks generated! This shouldn't happen."
                        )
                    else:
                        self._logger.trace(
                            f"Completion:\n{inference.content.model_dump_json(indent=2)}"
                        )

                    matches = []

                    for match in inference.content.checks:
                        if match.should_apply:
                            self._logger.debug(f"Activated:\n{match.model_dump_json(indent=2)}")

                            matches.append(
                                GuidelineMatch(
                                    guideline=self._guidelines[match.guideline_id],
                                    score=10 if match.should_apply else 1,
                                    rationale=match.tldr,
                                )
                            )
                        else:
                            self._logger.debug(f"Skipped:\n{match.model_dump_json(indent=2)}")

                    return GuidelineMatchingBatchResult(
                        matches=matches,
                        generation_info=inference.info,
                    )

            except Exception as exc:
                self._logger.warning(
                    f"Attempt {generation_attempt} failed: {traceback.format_exception(exc)}"
                )

                last_generation_exception = exc

        raise GuidelineMatchingBatchError() from last_generation_exception

    async def shots(
        self,
    ) -> Sequence[GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot]:
        return await shot_collection.list()

    def _format_shots(
        self,
        shots: Sequence[GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot],
    ) -> str:
        return "\n".join(
            f"示例 #{i}：###\n{self._format_shot(shot)}" for i, shot in enumerate(shots, start=1)
        )

    def _format_shot(
        self, shot: GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot
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
                f"{i}) {g.condition}" for i, g in enumerate(shot.guidelines, start=1)
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

    def _build_prompt(
        self,
        shots: Sequence[GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot],
    ) -> PromptBuilder:
        guideline_representations = {
            g.id: internal_representation(g) for g in self._guidelines.values()
        }

        guidelines_text = "\n".join(
            f"{i}) 条件：{guideline_representations[g.id].condition}。动作：{guideline_representations[g.id].action}"
            for i, g in self._guidelines.items()
        )

        builder = PromptBuilder(on_build=lambda prompt: self._logger.trace(f"Prompt:\n{prompt}"))

        builder.add_section(
            name="guideline-previously-applied-general-instructions",
            template="""
总体说明
-----------------
本系统中，对话 AI 客服的行为由「指导原则」指导。客服在与用户（也称客户）交互时会使用这些指导原则。
每条指导原则由两部分组成：
-「条件」：用自然语言描述指导原则何时适用。我们根据对话任一状态检查该条件，以判断该指导原则是否应参与生成对用户的下一条回复。
-「动作」：当指导原则的「条件」在对话特定状态下成立时，客服应遵循的自然语言指令。此处描述仅针对客服，不针对用户。

动作只能指示客服执行某事，但某些指导原则可能需要客户提供信息才能完成。这些称为「依赖客户」指导原则。
例如，动作「获取客户 ID 号」要求客服询问客户的账号，但只有用户提供后该指导原则才算完全完成。

任务说明
----------------

你的任务是评估一组「依赖客户」指导原则是否应适用于 AI 客服与用户之间对话的当前状态。

你将获得客服已在交互中至少执行过一次其部分动作的指导原则。现在需要根据对话当前状态判断每条指导原则是否应重新适用。

若满足以下任一条件，则应适用该指导原则：

   1. 动作未完成：原条件仍成立，触发客服初始动作的原因仍然相关，且客户尚未履行其部分动作。例如：客服已询问用户 ID，但用户尚未回复，且对话仍在讨论访问其账户。
   2. 相同条件的新上下文：条件在新上下文中再次出现，需客服和客户均重复该动作。例如：用户转而询问第二个账户，因此客服需再次询问 ID。

关键评估规则：

- 避免重复静态信息请求：除非确有新上下文，否则不要重新适用请求静态信息（ID、姓名、出生日期）的指导原则。但若动作同时包含静态和动态部分（如「询问姓名和偏好预约时间」），则当动态部分再次相关时应重新适用。

- 关注最近上下文：主要依据最后一条用户消息评估。仅当该最新消息中条件明确满足时才重新适用，而非基于对话更早部分。

- 处理上下文转换：若用户简要提及会触发指导原则的内容但随即在同一消息内换题，则不要认为条件成立。

- 跟踪解决状态：若条件最近一次实例已得到处理并解决，则无需重新适用。但若用户仍在处理未解决问题或出现新实例，则可能适合重新适用。


""",
            props={},
        )
        builder.add_section(
            name="guideline-matcher-examples-of-previously-applied-evaluations",
            template="""
指导原则匹配评估示例：
-------------------
{formatted_shots}
""",
            props={
                "formatted_shots": self._format_shots(shots),
                "shots": shots,
            },
        )
        builder.add_agent_identity(self._context.agent)
        builder.add_context_variables(self._context.context_variables)
        builder.add_glossary(self._context.terms)
        builder.add_capabilities_for_guideline_matching(self._context.capabilities)
        builder.add_customer_identity(self._context.customer, self._context.session)
        builder.add_interaction_history(self._context.interaction_history)
        builder.add_staged_tool_events(self._context.staged_events)
        builder.add_section(
            name=BuiltInSection.GUIDELINES,
            template="""
- 指导原则列表：###
{guidelines_text}
###
""",
            props={
                "guidelines_text": guidelines_text,
                "guidelines": [
                    {"condition": g.content.condition, "action": g.content.action}
                    for g in self._guidelines.values()
                ],
            },
            status=SectionStatus.ACTIVE,
        )

        builder.add_section(
            name="guideline-previously-applied-output-format",
            template="""
重要：列表中恰好有 {guidelines_len} 条指导原则需要你检查。

输出格式
-----------------
- 按说明填写下列列表中每条指导原则的适用性：
```json
{result_structure_text}
```
""",
            props={
                "result_structure_text": self._format_of_guideline_check_json_description(
                    guideline_representations=guideline_representations,
                ),
                "guidelines_len": len(self._guidelines),
            },
        )

        return builder

    def _format_of_guideline_check_json_description(
        self,
        guideline_representations: dict[GuidelineId, GuidelineInternalRepresentation],
    ) -> str:
        result_structure = [
            {
                "guideline_id": i,
                "condition": guideline_representations[g.id].condition,
                "action": guideline_representations[g.id].action,
                "condition_still_met": "<BOOL，引发该指导原则的条件在最近交互中是否仍相关且主题未变>",
                "customer_should_reply": "<BOOL，仅当 condition_still_met=True 时包含。客户是否需要履行其部分动作>",
                "condition_met_again": "<BOOL，仅当 customer_should_reply=False 时包含。条件是否因新原因在最近交互中再次满足且动作应再次执行>",
                "action_should_reapply": "<BOOL，仅当 condition_met_again=True 时包含。动作是否非静态且应再次执行>",
                "action_wasnt_taken": "<BOOL，仅当 action_should_reapply=True 时包含。新动作是否尚未由客服或客户执行>",
                "tldr": "<str，说明为何该指导原则应在最近上下文中适用>",
                "should_apply": "<BOOL>",
            }
            for i, g in self._guidelines.items()
        ]
        result = {"checks": result_structure}
        return json.dumps(result, indent=4)


class GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatching(
    GuidelineMatchingStrategy
):
    def __init__(
        self,
        logger: Logger,
        meter: Meter,
        optimization_policy: OptimizationPolicy,
        entity_queries: EntityQueries,
        schematic_generator: SchematicGenerator[
            GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema
        ],
    ) -> None:
        self._logger = logger
        self._meter = meter
        self._optimization_policy = optimization_policy
        self._entity_queries = entity_queries
        self._schematic_generator = schematic_generator

    @override
    async def create_matching_batches(
        self,
        guidelines: Sequence[Guideline],
        context: GuidelineMatchingContext,
    ) -> Sequence[GuidelineMatchingBatch]:
        journeys = (
            self._entity_queries.guideline_and_journeys_it_depends_on.get(guidelines[0].id, [])
            if guidelines
            else []
        )

        batches = []

        guidelines_dict = {g.id: g for g in guidelines}
        batch_size = self._get_optimal_batch_size(guidelines_dict)
        guidelines_list = list(guidelines_dict.items())
        batch_count = math.ceil(len(guidelines_dict) / batch_size)

        for batch_number in range(batch_count):
            start_offset = batch_number * batch_size
            end_offset = start_offset + batch_size
            batch = dict(guidelines_list[start_offset:end_offset])
            batches.append(
                self._create_batch(
                    guidelines=list(batch.values()),
                    journeys=journeys,
                    context=GuidelineMatchingContext(
                        agent=context.agent,
                        session=context.session,
                        customer=context.customer,
                        context_variables=context.context_variables,
                        interaction_history=context.interaction_history,
                        terms=context.terms,
                        capabilities=context.capabilities,
                        staged_events=context.staged_events,
                        active_journeys=journeys,
                        journey_paths=context.journey_paths,
                    ),
                )
            )

        return batches

    def _get_optimal_batch_size(
        self,
        guidelines: dict[GuidelineId, Guideline],
    ) -> int:
        return self._optimization_policy.get_guideline_matching_batch_size(
            len(guidelines),
            hints={
                "type": GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingBatch
            },
        )

    def _create_batch(
        self,
        guidelines: Sequence[Guideline],
        journeys: Sequence[Journey],
        context: GuidelineMatchingContext,
    ) -> GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingBatch:
        return GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingBatch(
            logger=self._logger,
            meter=self._meter,
            optimization_policy=self._optimization_policy,
            schematic_generator=self._schematic_generator,
            guidelines=guidelines,
            journeys=journeys,
            context=context,
        )


def _make_event(e_id: str, source: EventSource, message: str) -> Event:
    return Event(
        id=EventId(e_id),
        source=source,
        kind=EventKind.MESSAGE,
        creation_utc=datetime.now(timezone.utc),
        offset=0,
        trace_id="",
        data={"message": message},
        metadata={},
        deleted=False,
    )


example_1_events = [
    _make_event(
        "11", EventSource.CUSTOMER, "I'm planning a trip next month. Any ideas on where to go?"
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "That sounds exciting! What kind of activities do you enjoy — relaxing on the beach, hiking, museums, food tours?",
    ),
    _make_event(
        "44", EventSource.CUSTOMER, "That's a complicated question. I will think and tell you."
    ),
]

example_1_guidelines = [
    GuidelineContent(
        condition="The customer wants recommendations for a trip",
        action="Ask for their preferred activities and recommend accordingly",
    ),
]

example_1_expected = GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema(
    checks=[
        GenericPreviouslyAppliedActionableCustomerDependentBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer wants recommendations for a trip",
            action="Ask for their preferred activities and recommend accordingly",
            condition_still_met=True,
            customer_should_reply=True,
            tldr="The customer should answer what's their preferred activities.",
            should_apply=True,
        ),
    ]
)


example_2_events = [
    _make_event(
        "11", EventSource.CUSTOMER, "I'm planning a trip next month. Any ideas on where to go?"
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "That sounds exciting! What kind of activities do you enjoy — relaxing on the beach, hiking, museums, food tours?",
    ),
    _make_event("25", EventSource.CUSTOMER, "I love hiking and exploring local food scenes."),
]

example_2_guidelines = [
    GuidelineContent(
        condition="The customer wants recommendations for a trip",
        action="Ask for their preferred activities and recommend accordingly",
    ),
]

example_2_expected = GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema(
    checks=[
        GenericPreviouslyAppliedActionableCustomerDependentBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer wants recommendations for a trip",
            action="Ask for their preferred activities and recommend accordingly",
            condition_still_met=True,
            customer_should_reply=False,
            condition_met_again=False,
            tldr="The customer has already answer what's their preferred activities",
            should_apply=False,
        ),
    ]
)

example_3_events = [
    _make_event(
        "11", EventSource.CUSTOMER, "I'm planning a trip next month. Any ideas on where to go?"
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "That sounds exciting! What kind of activities do you enjoy—relaxing on the beach, hiking, museums, food tours?",
    ),
    _make_event("66", EventSource.CUSTOMER, "I love hiking and exploring local food scenes."),
    _make_event(
        "76",
        EventSource.AI_AGENT,
        "Great! You might enjoy a trip to the Pacific Northwest—plenty of trails and great food in Portland and Seattle.",
    ),
    _make_event("89", EventSource.CUSTOMER, "What about a winter trip in Europe?"),
]

example_3_guidelines = [
    GuidelineContent(
        condition="The customer wants recommendations for a trip",
        action="Ask for their preferred activities and recommend accordingly",
    ),
]

example_3_expected = GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema(
    checks=[
        GenericPreviouslyAppliedActionableCustomerDependentBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer wants recommendations for a trip",
            action="Ask for their preferred activities and recommend accordingly",
            condition_still_met=True,
            customer_should_reply=False,
            condition_met_again=True,
            action_should_reapply=True,
            action_wasnt_taken=True,
            tldr="The customer ask about a new trip plan.",
            should_apply=True,
        ),
    ]
)


example_4_events = [
    _make_event(
        "11", EventSource.CUSTOMER, "I'm planning a trip next month. Any ideas on where to go?"
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "That sounds exciting! What kind of activities do you enjoy—relaxing on the beach, hiking, museums, food tours?",
    ),
    _make_event("26", EventSource.CUSTOMER, "I love hiking and exploring local food scenes."),
    _make_event(
        "54",
        EventSource.AI_AGENT,
        "Great! You might enjoy a trip to the Pacific Northwest—plenty of trails and great food in Portland and Seattle.",
    ),
    _make_event("66", EventSource.CUSTOMER, "What about a winter trip in Europe?"),
    _make_event(
        "77",
        EventSource.AI_AGENT,
        "That can be great! What kind of activities would you like to do there?",
    ),
    _make_event("78", EventSource.CUSTOMER, "I will go to France probably"),
]

example_4_guidelines = [
    GuidelineContent(
        condition="The customer wants recommendations for a trip",
        action="Ask for their preferred activities and recommend accordingly",
    ),
]

example_4_expected = GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema(
    checks=[
        GenericPreviouslyAppliedActionableCustomerDependentBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer wants recommendations for a trip",
            action="Ask for their preferred activities and recommend accordingly",
            condition_still_met=True,
            customer_should_reply=True,
            tldr="The customer didn't answer the question.",
            should_apply=True,
        ),
    ]
)

example_5_events = [
    _make_event(
        "11", EventSource.CUSTOMER, "I'm planning a trip next month. Any ideas on where to go?"
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "That sounds exciting! What kind of activities do you enjoy—relaxing on the beach, hiking, museums, food tours?",
    ),
    _make_event("26", EventSource.CUSTOMER, "I love hiking and exploring local food scenes."),
    _make_event(
        "54",
        EventSource.AI_AGENT,
        "Great! You might enjoy a trip to the Pacific Northwest—plenty of trails and great food in Portland and Seattle.",
    ),
    _make_event("66", EventSource.CUSTOMER, "What about a winter trip in Europe?"),
    _make_event(
        "77",
        EventSource.AI_AGENT,
        "That can be great! What kind of activities would you like to do there?",
    ),
    _make_event("78", EventSource.CUSTOMER, "Actually let's stick to the Plan for next month"),
]

example_5_guidelines = [
    GuidelineContent(
        condition="The customer wants recommendations for a trip",
        action="Ask for their preferred activities and recommend accordingly",
    ),
]

example_5_expected = GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema(
    checks=[
        GenericPreviouslyAppliedActionableCustomerDependentBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer wants recommendations for a trip",
            action="Ask for their preferred activities and recommend accordingly",
            condition_still_met=False,
            tldr="The customer regret about the new planning",
            should_apply=False,
        ),
    ]
)


example_6_events = [
    _make_event("11", EventSource.CUSTOMER, "Hi, I need help changing the email on my account."),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "Sure! Could you please provide your account ID so I can verify your identity?",
    ),
    _make_event("26", EventSource.CUSTOMER, "It’s ACC12345."),
    _make_event(
        "54",
        EventSource.AI_AGENT,
        "Thanks! I’ve updated your email.",
    ),
    _make_event("66", EventSource.CUSTOMER, "Also, can you check the last payment on my account?"),
]

example_6_guidelines = [
    GuidelineContent(
        condition="The customer is asking for account-related help",
        action="Ask for their account ID to verify their identity",
    ),
]

example_6_expected = GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchesSchema(
    checks=[
        GenericPreviouslyAppliedActionableCustomerDependentBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer is asking for account-related help",
            action="Ask for their account ID to verify their identity",
            condition_still_met=True,
            customer_should_reply=False,
            condition_met_again=True,
            action_should_reapply=False,
            tldr="The customer already provided their account Id",
            should_apply=False,
        ),
    ]
)

_baseline_shots: Sequence[
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot
] = [
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot(
        description="",
        interaction_events=example_1_events,
        guidelines=example_1_guidelines,
        expected_result=example_1_expected,
    ),
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot(
        description="",
        interaction_events=example_2_events,
        guidelines=example_2_guidelines,
        expected_result=example_2_expected,
    ),
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot(
        description="",
        interaction_events=example_3_events,
        guidelines=example_3_guidelines,
        expected_result=example_3_expected,
    ),
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot(
        description="",
        interaction_events=example_4_events,
        guidelines=example_4_guidelines,
        expected_result=example_4_expected,
    ),
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot(
        description="",
        interaction_events=example_5_events,
        guidelines=example_5_guidelines,
        expected_result=example_5_expected,
    ),
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot(
        description="",
        interaction_events=example_6_events,
        guidelines=example_6_guidelines,
        expected_result=example_6_expected,
    ),
]

shot_collection = ShotCollection[
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingShot
](_baseline_shots)
