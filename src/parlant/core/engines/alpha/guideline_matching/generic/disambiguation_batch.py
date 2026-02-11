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

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import traceback
import json
from typing import Optional
from typing_extensions import override
from parlant.core.common import DefaultBaseModel, JSONSerializable
from parlant.core.engines.alpha.guideline_matching.common import measure_guideline_matching_batch
from parlant.core.engines.alpha.guideline_matching.generic.common import (
    internal_representation,
)
from parlant.core.engines.alpha.guideline_matching.guideline_match import (
    GuidelineMatch,
)
from parlant.core.engines.alpha.guideline_matching.guideline_matcher import (
    GuidelineMatchingBatch,
    GuidelineMatchingBatchResult,
    GuidelineMatchingBatchError,
)
from parlant.core.engines.alpha.guideline_matching.guideline_matching_context import (
    GuidelineMatchingContext,
)
from parlant.core.engines.alpha.optimization_policy import OptimizationPolicy
from parlant.core.engines.alpha.prompt_builder import BuiltInSection, PromptBuilder, SectionStatus
from parlant.core.guidelines import Guideline, GuidelineContent, GuidelineId
from parlant.core.journeys import JourneyId, JourneyStore
from parlant.core.loggers import Logger
from parlant.core.meter import Meter
from parlant.core.nlp.generation import SchematicGenerator
from parlant.core.sessions import Event, EventId, EventKind, EventSource
from parlant.core.shots import Shot, ShotCollection
from parlant.core.tags import Tag


class GuidelineCheck(DefaultBaseModel):
    guideline_id: str
    tldr: str
    requires_disambiguation: bool


class DisambiguationGuidelineMatchesSchema(DefaultBaseModel):
    tldr: str
    ambiguity_condition_met: bool
    disambiguation_requested: bool
    customer_resolved: Optional[bool] = False
    is_ambiguous: bool
    guidelines: Optional[list[GuidelineCheck]] = []
    clarification_action: Optional[str] = ""


@dataclass
class DisambiguationGuidelineMatchingShot(Shot):
    interaction_events: Sequence[Event]
    disambiguation_condition: GuidelineContent
    disambiguation_targets: Sequence[GuidelineContent]
    expected_result: DisambiguationGuidelineMatchesSchema


@dataclass
class _Guideline:
    conditions: list[str]
    action: str | None
    ids: list[GuidelineId]


class GenericDisambiguationGuidelineMatchingBatch(GuidelineMatchingBatch):
    def __init__(
        self,
        logger: Logger,
        meter: Meter,
        journey_store: JourneyStore,
        optimization_policy: OptimizationPolicy,
        schematic_generator: SchematicGenerator[DisambiguationGuidelineMatchesSchema],
        disambiguation_guideline: Guideline,
        disambiguation_targets: Sequence[Guideline],
        context: GuidelineMatchingContext,
    ) -> None:
        self._logger = logger
        self._meter = meter

        self._journey_store = journey_store
        self._optimization_policy = optimization_policy
        self._schematic_generator = schematic_generator
        self._disambiguation_guideline = disambiguation_guideline
        self._disambiguation_targets = disambiguation_targets
        self._context = context

    @property
    @override
    def size(self) -> int:
        return 1

    async def _get_disambiguation_targets(
        self,
        disambiguation_targets: Sequence[Guideline],
    ) -> dict[str, _Guideline]:
        journey_to_conditions = defaultdict(list)
        guidelines_targets = []
        for g in disambiguation_targets:
            for t in g.tags:
                if journey_id := Tag.extract_journey_id(t):
                    journey_to_conditions[journey_id].append(g)
                else:
                    guidelines_targets.append(g)
                    continue
            if not g.tags:
                guidelines_targets.append(g)

        guidelines = {}
        i = 1
        for journey_id, conditions in journey_to_conditions.items():
            journey = await self._journey_store.read_journey(JourneyId(journey_id))
            guidelines[str(i)] = _Guideline(
                conditions=[g.content.condition for g in conditions],
                action=journey.title,
                ids=[g.id for g in conditions],
            )
            i += 1
        for g in guidelines_targets:
            guidelines[str(i)] = _Guideline(
                conditions=[internal_representation(g).condition],
                action=internal_representation(g).action,
                ids=[g.id],
            )
            i += 1
        return guidelines

    @override
    async def process(self) -> GuidelineMatchingBatchResult:
        disambiguation_targets_guidelines = await self._get_disambiguation_targets(
            self._disambiguation_targets
        )

        async with measure_guideline_matching_batch(self._meter, self):
            prompt = self._build_prompt(
                shots=await self.shots(),
                disambiguation_targets_guidelines=disambiguation_targets_guidelines,
            )

            generation_attempt_temperatures = (
                self._optimization_policy.get_guideline_matching_batch_retry_temperatures(
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

                    self._logger.trace(
                        f"Completion:\n{inference.content.model_dump_json(indent=2)}"
                    )

                    metadata: dict[str, JSONSerializable] = {}

                    if inference.content.is_ambiguous:
                        guidelines: list[str] = []
                        for g in inference.content.guidelines or []:
                            if g.requires_disambiguation:
                                guidelines.extend(
                                    disambiguation_targets_guidelines[g.guideline_id].ids
                                )

                        disambiguation_data: JSONSerializable = {
                            "targets": guidelines,
                            "enriched_action": inference.content.clarification_action or "",
                        }

                        metadata["disambiguation"] = disambiguation_data

                        self._logger.debug(
                            f"Disambiguation activated: {inference.content.model_dump_json(indent=2)}"
                        )

                    matches = [
                        GuidelineMatch(
                            guideline=self._disambiguation_guideline,
                            score=10 if inference.content.is_ambiguous else 1,
                            rationale=f'''消歧理由：「{inference.content.tldr}」''',
                            metadata=metadata,
                        )
                    ]

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

    async def shots(self) -> Sequence[DisambiguationGuidelineMatchingShot]:
        return await shot_collection.list()

    def _format_shots(
        self,
        shots: Sequence[DisambiguationGuidelineMatchingShot],
    ) -> str:
        return "\n".join(
            f"""
示例 {i} - {shot.description}：###
{self._format_shot(shot)}
###
"""
            for i, shot in enumerate(shots, start=1)
        )

    def _format_shot(self, shot: DisambiguationGuidelineMatchingShot) -> str:
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
        if shot.disambiguation_condition:
            formatted_shot += f"""
- **消歧条件**：
{shot.disambiguation_condition.condition}

"""
        if shot.disambiguation_targets:
            formatted_guidelines = "\n".join(
                f"{i}) 条件：{g.condition}。动作：{g.action}"
                for i, g in enumerate(shot.disambiguation_targets, start=1)
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
        disambiguation_targets_guidelines: dict[str, _Guideline],
        shots: Sequence[DisambiguationGuidelineMatchingShot],
    ) -> PromptBuilder:
        disambiguation_condition_internal = internal_representation(self._disambiguation_guideline)

        disambiguation_targets_text = "\n".join(
            f"{id}) 条件：{', '.join(g.conditions) if len(g.conditions) > 1 else g.conditions[0]}。"
            f"动作：{g.action}"
            for id, g in disambiguation_targets_guidelines.items()
        )
        builder = PromptBuilder(on_build=lambda prompt: self._logger.trace(f"Prompt:\n{prompt}"))

        builder.add_section(
            name="guideline-disambiguation-evaluator-general-instructions",
            template="""
总体说明
-----------------
本系统中，对话 AI 客服的行为由「指导原则」指导。客服在与客户（也称用户）交互时会使用这些指导原则。
每条指导原则由两部分组成：
-「条件」：用自然语言描述指导原则何时适用。我们根据对话最近状态评估该条件，以判断该指导原则是否应参与生成对客户的下一条回复。
-「动作」：当指导原则的「条件」在对话最新状态下成立时，客服应遵循的自然语言指令。此处描述仅针对客服，不针对客户。

任务说明
----------------
在与客户交互时，客户可能表达可由多条指导原则处理的需求或问题，从而产生歧义。
当多条指导原则条件可能同时适用，但信息不足以确定应适用哪一条时，即出现此情况。
此时，我们需要识别可能相关的指导原则，并询问客户他们指的是哪一条。

你的任务是判断客户意图在给定的消歧条件及相关指导原则下是否存在歧义；若存在，则确定可能的理解或方向。
你将获得：
1. 歧义条件：当为真时表示潜在歧义
2. 相关指导原则列表，每条代表客户可能选择的一条路径

评估歧义条件是否在当前交互上下文中成立。
若成立，评估是否有多条指导原则的条件与用户的询问相关。
若存在歧义（歧义条件为真且多条指导原则适用）：
    - 识别代表可选方案的相关指导原则。简要说明用户的请求如何可被解读为该指导原则相关。
    - 按以下格式拟定回复：
    「询问客户是想要 X、Y 还是 Z…」
    该回复应清晰呈现选项以帮助消除歧义。

关于识别真实歧义：
- 若歧义与正在评估的指导原则无直接关联，或歧义范围超出当前评估的消歧条件，则不要标为歧义。
- 指导原则往往以细微差别描述非常相似的请求。若客户已表明哪一选项与其相关，则不存在歧义——即使你认为另一类似指导原则也可能适用。
当客户已明确表达其需求时，不要检测歧义。应信任客户表述的意图，而非猜测他们可能指的是类似替代。
仅当客户请求确实不清、且可能合理匹配多条不同路径时才进行消歧。
    例如：若指导原则同时包含「退货退款」和「退货换货」，而客户说「我想退货退款」，则不要询问是否指换货。客户已明确表达意图。
- 当存在歧义时，包含所有可能适用的指导原则——让客户在所有可行选项中做选择。
- 某些指导原则可能根据交互被判定为无关。例如，因对话更早部分或用户状态（来自交互历史或上下文变量）而排除。若仅剩一条或没有相关指导原则，则不存在歧义。

消歧已被询问后：
- 若你已向客户请求消歧，请**特别注意**是否需要再次请求澄清，抑或用户已回复且歧义已消除。
- **接受简短回复为有效澄清**：客户常以很短回复交流（单词或短语如「退货」「换货」「是」「否」）。若客户简短回复明确表明其在先前选项中做出的选择，即使回答不是完整句子，也应视为歧义已消除。
- 仔细区分以下情况：
  1. 已请求消歧且待澄清（客服已询问消歧，但客户尚未回答）——此情况下应重新消歧（设 disambiguation_requested = true、customer_resolved=false、is_ambiguous = true）
  2. 已请求消歧且已提供澄清（客户已回答）——不要对同一问题再次消歧（disambiguation_requested = true、customer_resolved=true、is_ambiguous = false）
  3. 新歧义（出现不同的不明意图）——应进行消歧（is_ambiguous = true）

关注当前上下文：
基于客户最近一条消息进行评估。若客户在最近消息中已换题或转向不同话题，不要对之前未解决的歧义进行消歧。
始终优先考虑客户当前请求和意图，而非过去的歧义。


""",
            props={},
        )
        builder.add_section(
            name="guideline-ambiguity-evaluations-examples",
            template="""
指导原则歧义评估示例：
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
- 歧义条件：###
{disambiguation_condition}
###
- 指导原则列表：###
{disambiguation_targets_text}
###
""",
            props={
                "disambiguation_targets_text": disambiguation_targets_text,
                "disambiguation_condition": disambiguation_condition_internal.condition,
            },
            status=SectionStatus.ACTIVE,
        )
        builder.add_section(
            name="guideline-disambiguation-evaluation-output-format",
            template="""

输出格式
-----------------
- 按说明填写下列列表中消歧评估的细节：
```json
{result_structure_text}
```
""",
            props={
                "result_structure_text": self._format_of_guideline_check_json_description(
                    disambiguation_targets_guidelines
                ),
            },
        )

        return builder

    def _format_of_guideline_check_json_description(
        self, disambiguation_targets_guidelines: dict[str, _Guideline]
    ) -> str:
        result = {
            "tldr": "<str，根据客户最新输入简要说明其最近意图，并解释为何相对于歧义条件和给定指导原则存在歧义>",
            "ambiguity_condition_met": "<BOOL，根据交互判断歧义条件是否满足>",
            "disambiguation_requested": "<BOOL，根据交互判断客服是否已请求澄清。若是，is_ambiguous 仅在客户未回答或客户更改请求或出现新歧义时为 true>",
            "customer_resolved": "<BOOL，当 disambiguation_requested=true 时包含。用户是否已解决最新请求的歧义>",
            "is_ambiguous": "<BOOL>",
            "guidelines (include only if is_ambiguous is True)": [
                {
                    "guideline_id": i,
                    "tldr": "<str，简要说明本指导原则是否需要消歧、是否明确相关或是否无关>",
                    "requires_disambiguation": "<BOOL，本指导原则是否相关且需参与消歧请求>",
                }
                for i in disambiguation_targets_guidelines.keys()
            ],
            "clarification_action": "<仅当 is_ambiguous 为 True 时包含。形如「询问用户是否想要…」的动作>",
        }
        return json.dumps(result, indent=4)


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
        "11",
        EventSource.CUSTOMER,
        "I received the wrong item in my order.",
    ),
]

example_1_disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to return an item for a refund",
        action="refund the order",
    ),
    GuidelineContent(
        condition="The customer asks to replace an item",
        action="Send the correct item and ask the customer to return the one they received",
    ),
]

example_1_disambiguation_condition = GuidelineContent(
    condition="The customer received a wrong or damaged item",
    action="-",
)

example_1_expected = DisambiguationGuidelineMatchesSchema(
    tldr="The customer claimed to receive the wrong item; may want to either replace it or get a refund.",
    ambiguity_condition_met=True,
    disambiguation_requested=False,
    is_ambiguous=True,
    guidelines=[
        GuidelineCheck(
            guideline_id="1",
            tldr="May want to refund the wrong item",
            requires_disambiguation=True,
        ),
        GuidelineCheck(
            guideline_id="2",
            tldr="May want to replace the wrong item",
            requires_disambiguation=True,
        ),
    ],
    clarification_action="ask the customer whether they'd prefer a replacement or a refund.",
)


example_2_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "Hey, can you book me an appointment? I need a prescription",
    ),
]

example_2__disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to book an appointment with a doctor",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book a session with a psychologist",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book an online appointment to a medical consultation or a session with a psychologist",
        action="book the appointment online",
    ),
]

example_2_disambiguation_condition = GuidelineContent(
    condition="The customer wants to book an appointment, but it's unclear whether it's with a doctor or a psychologist, and whether it should be online or in-person.",
    action="-",
)

example_2_expected = DisambiguationGuidelineMatchesSchema(
    tldr="The customer asks to book an appointment but didn't specify the type or the place. Since they mention needing a prescription, it likely relates to a medical consultation, not a psychological one.",
    ambiguity_condition_met=True,
    disambiguation_requested=False,
    is_ambiguous=True,
    guidelines=[
        GuidelineCheck(
            guideline_id="1",
            tldr="The appointment is with a doctor since they mentioned a prescription",
            requires_disambiguation=True,
        ),
        GuidelineCheck(
            guideline_id="2",
            tldr="A psychologist is not relevant, they cannot prescribe medication.",
            requires_disambiguation=False,
        ),
        GuidelineCheck(
            guideline_id="3",
            tldr="An online appointment can be relevant",
            requires_disambiguation=True,
        ),
    ],
    clarification_action="Ask the customer if they prefer an online or in-person doctor's appointment",
)


example_3_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "Hey, can you help me?",
    ),
]

example_3__disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to book an appointment with a doctor",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book a session with a psychologist",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book an online appointment to a medical consultation or a session with a psychologist",
        action="book the appointment online",
    ),
]

example_3_disambiguation_condition = GuidelineContent(
    condition="The customer asked to book an appointment, but it's unclear whether it's with a doctor or a psychologist, and whether it should be online or in-person.",
    action="-",
)

example_3_expected = DisambiguationGuidelineMatchesSchema(
    tldr="The customer asked for help and didn't specify with what. However, they did not specified that they need help with book an appointment so the ambiguity condition is not met.",
    ambiguity_condition_met=False,
    disambiguation_requested=False,
    is_ambiguous=False,
)


example_4_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "Hey, are you offering in-person sessions these days, or is everything online?",
    ),
    _make_event(
        "15",
        EventSource.AI_AGENT,
        "I'm sorry, but due to the current situation, we aren't holding in-person meetings. However, we do offer online sessions if needed",
    ),
    _make_event(
        "20",
        EventSource.CUSTOMER,
        "Got it. I'll need an appointment — my throat is sore.",
    ),
]

example_4__disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to book an appointment with a doctor",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book a session with a psychologist",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book an online appointment to a medical consultation or a session with a psychologist",
        action="book the appointment online",
    ),
]

example_4_disambiguation_condition = GuidelineContent(
    condition="The customer wants to book an appointment, but it's unclear whether it's with a doctor or a psychologist, and whether it should be online or in-person.",
    action="-",
)

example_4_expected = DisambiguationGuidelineMatchesSchema(
    tldr="The customer asks to book an appointment. Online sessions are not available. Since they mention a sore throat, it likely relates to a medical consultation, not a psychologist.",
    ambiguity_condition_met=False,
    disambiguation_requested=False,
    is_ambiguous=False,
)


example_5_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "Hey, can you book me an appointment? I need a prescription",
    ),
    _make_event(
        "14",
        EventSource.AI_AGENT,
        "You can have a doctor's session either in-person or online. Which do you prefer?",
    ),
    _make_event(
        "17",
        EventSource.CUSTOMER,
        "I can do it online. Also, I need to book an appointment for my daughter.",
    ),
]

example_5_disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to book an appointment with a doctor",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book a session with a psychologist",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book an online appointment to a medical consultation or a session with a psychologist",
        action="book the appointment online",
    ),
]

example_5_disambiguation_condition = GuidelineContent(
    condition="The customer wants to book an appointment, but it's unclear whether it's with a doctor or a psychologist, and whether it should be online or in-person.",
    action="-",
)

example_5_expected = DisambiguationGuidelineMatchesSchema(
    tldr="Based on latest message, there is a new request which is again ambiguous. Need to clarify whether it's with a doctor or a psychologist, and whether it should be online or in-person",
    ambiguity_condition_met=True,
    disambiguation_requested=False,
    is_ambiguous=True,
    guidelines=[
        GuidelineCheck(
            guideline_id="1",
            tldr="The appointment may be with a doctor",
            requires_disambiguation=True,
        ),
        GuidelineCheck(
            guideline_id="2",
            tldr="Psychologist may be relevant",
            requires_disambiguation=True,
        ),
        GuidelineCheck(
            guideline_id="3",
            tldr="An Online appointment can be relevant",
            requires_disambiguation=True,
        ),
    ],
    clarification_action="Ask the customer if they need a doctor or psychologist appointment and if they prefer an online or in-person session for their daughter",
)


example_6_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "Hey, can you book me an appointment? I need a prescription. And also I need a session with a psychologist with my wife in your office.",
    ),
]

example_6__disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to book an appointment with a doctor",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book a session with a psychologist",
        action="book the appointment",
    ),
    GuidelineContent(
        condition="The customer asks to book an online appointment to a medical consultation or a session with a psychologist",
        action="book the appointment online",
    ),
    GuidelineContent(
        condition="The customer asks to book an in-person appointment to a medical consultation or a session with a psychologist",
        action="book the in-person appointment",
    ),
]

example_6_disambiguation_condition = GuidelineContent(
    condition="The customer wants to book an appointment, but it's unclear whether it should be online or in-person. They say prescription so they need a doctor.",
    action="-",
)

example_6_expected = DisambiguationGuidelineMatchesSchema(
    tldr="The customer asked to book two appointments. For the first appointment there is an ambiguity between doctor or psychologist, and online or in-person. The second one is clear.",
    ambiguity_condition_met=True,
    disambiguation_requested=False,
    is_ambiguous=True,
    guidelines=[
        GuidelineCheck(
            guideline_id="1",
            tldr="They ask for prescription so they need a doctor appointment, no ambiguity",
            requires_disambiguation=False,
        ),
        GuidelineCheck(
            guideline_id="2",
            tldr="Psychologist can't be relevant for getting a prescription",
            requires_disambiguation=False,
        ),
        GuidelineCheck(
            guideline_id="3",
            tldr="Online appointment can be relevant for getting a prescription",
            requires_disambiguation=True,
        ),
        GuidelineCheck(
            guideline_id="4",
            tldr="In-person appointment can be relevant for getting a prescription",
            requires_disambiguation=True,
        ),
    ],
    clarification_action="Ask the customer if they prefer an online or in-person session for the appointment for getting a prescription",
)


example_7_events = [
    _make_event(
        "1",
        EventSource.CUSTOMER,
        "I received the wrong item in my order. This isn't what I ordered at all.",
    ),
    _make_event(
        "2",
        EventSource.AI_AGENT,
        "I'm sorry to hear you received the wrong item. Would you prefer a replacement of the correct item or a refund?",
    ),
    _make_event(
        "3",
        EventSource.CUSTOMER,
        "replace",
    ),
]

example_7_disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to return an item for a refund",
        action="refund the order",
    ),
    GuidelineContent(
        condition="The customer asks to replace an item",
        action="Send the correct item and ask the customer to return the one they received",
    ),
]

example_7_disambiguation_condition = GuidelineContent(
    condition="The customer received a wrong or damaged item",
    action="-",
)

example_7_expected = DisambiguationGuidelineMatchesSchema(
    tldr="The customer received a wrong item and was asked whether they wanted a replacement or refund. They responded with 'replace', which clearly indicates their choice and resolves the ambiguity.",
    ambiguity_condition_met=False,
    disambiguation_requested=True,
    customer_resolved=True,
    is_ambiguous=False,
)

example_8_events = [
    _make_event(
        "1",
        EventSource.CUSTOMER,
        "I received the wrong item in my order. This isn't what I ordered at all.",
    ),
    _make_event(
        "2",
        EventSource.AI_AGENT,
        "I'm sorry to hear you received the wrong item. Would you prefer a replacement of the correct item or a refund?",
    ),
    _make_event(
        "3",
        EventSource.CUSTOMER,
        "I need to think.",
    ),
]

example_8_disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to return an item for a refund",
        action="refund the order",
    ),
    GuidelineContent(
        condition="The customer asks to replace an item",
        action="Send the correct item and ask the customer to return the one they received",
    ),
]

example_8_disambiguation_condition = GuidelineContent(
    condition="The customer received a wrong or damaged item",
    action="-",
)

example_8_expected = DisambiguationGuidelineMatchesSchema(
    tldr="The customer received a wrong item and clarification was asked. The customer only said that they need to think so the ambiguity still applies",
    ambiguity_condition_met=True,
    disambiguation_requested=True,
    customer_resolved=False,
    is_ambiguous=True,
    guidelines=[
        GuidelineCheck(
            guideline_id="1",
            tldr="may want to refund the wrong item",
            requires_disambiguation=True,
        ),
        GuidelineCheck(
            guideline_id="2",
            tldr="may want to replace the wrong item",
            requires_disambiguation=True,
        ),
    ],
    clarification_action="ask the customer whether they'd prefer a replacement or a refund.",
)


example_9_events = [
    _make_event(
        "1",
        EventSource.CUSTOMER,
        "I received the wrong item in my order. This isn't what I ordered at all.",
    ),
    _make_event(
        "2",
        EventSource.AI_AGENT,
        "I'm sorry to hear you received the wrong item. Would you prefer a replacement of the correct item or a refund?",
    ),
    _make_event(
        "3",
        EventSource.CUSTOMER,
        "I need to decide, I'm not sure. I will let you know. But can you help me please make a new order? I need new running shoes",
    ),
]

example_9_disambiguation_targets = [
    GuidelineContent(
        condition="The customer asks to return an item for a refund",
        action="refund the order",
    ),
    GuidelineContent(
        condition="The customer asks to replace an item",
        action="Send the correct item and ask the customer to return the one they received",
    ),
]

example_9_disambiguation_condition = GuidelineContent(
    condition="The customer received a wrong or damaged item",
    action="-",
)

example_9_expected = DisambiguationGuidelineMatchesSchema(
    tldr="The customer received a wrong item and clarification was asked. The customer did not clarify how to handle the wrong item but they changed the subject so no disambiguation is needed according to the most recent context",
    ambiguity_condition_met=True,
    disambiguation_requested=True,
    customer_resolved=False,
    is_ambiguous=False,
)

_baseline_shots: Sequence[DisambiguationGuidelineMatchingShot] = [
    DisambiguationGuidelineMatchingShot(
        description="Disambiguation example",
        interaction_events=example_1_events,
        disambiguation_targets=example_1_disambiguation_targets,
        disambiguation_condition=example_1_disambiguation_condition,
        expected_result=example_1_expected,
    ),
    DisambiguationGuidelineMatchingShot(
        description="Disambiguation example when not all guidelines are relevant",
        interaction_events=example_2_events,
        disambiguation_targets=example_2__disambiguation_targets,
        disambiguation_condition=example_2_disambiguation_condition,
        expected_result=example_2_expected,
    ),
    DisambiguationGuidelineMatchingShot(
        description="Non disambiguation example",
        interaction_events=example_3_events,
        disambiguation_targets=example_3__disambiguation_targets,
        disambiguation_condition=example_3_disambiguation_condition,
        expected_result=example_3_expected,
    ),
    DisambiguationGuidelineMatchingShot(
        description="Disambiguation resolves based on the interaction",
        interaction_events=example_4_events,
        disambiguation_targets=example_4__disambiguation_targets,
        disambiguation_condition=example_4_disambiguation_condition,
        expected_result=example_4_expected,
    ),
    DisambiguationGuidelineMatchingShot(
        description="New ambiguous request",
        interaction_events=example_5_events,
        disambiguation_targets=example_5_disambiguation_targets,
        disambiguation_condition=example_5_disambiguation_condition,
        expected_result=example_5_expected,
    ),
    DisambiguationGuidelineMatchingShot(
        description="Several requests, one needs disambiguation",
        interaction_events=example_6_events,
        disambiguation_targets=example_6__disambiguation_targets,
        disambiguation_condition=example_6_disambiguation_condition,
        expected_result=example_6_expected,
    ),
    DisambiguationGuidelineMatchingShot(
        description="Disambiguation applied and clarified",
        interaction_events=example_7_events,
        disambiguation_targets=example_7_disambiguation_targets,
        disambiguation_condition=example_7_disambiguation_condition,
        expected_result=example_7_expected,
    ),
    DisambiguationGuidelineMatchingShot(
        description="Disambiguation applied and customer did not respond",
        interaction_events=example_8_events,
        disambiguation_targets=example_8_disambiguation_targets,
        disambiguation_condition=example_8_disambiguation_condition,
        expected_result=example_8_expected,
    ),
    DisambiguationGuidelineMatchingShot(
        description="Disambiguation applied and customer did not respond but changed subject. No disambiguation required",
        interaction_events=example_9_events,
        disambiguation_targets=example_9_disambiguation_targets,
        disambiguation_condition=example_9_disambiguation_condition,
        expected_result=example_9_expected,
    ),
]

shot_collection = ShotCollection[DisambiguationGuidelineMatchingShot](_baseline_shots)
