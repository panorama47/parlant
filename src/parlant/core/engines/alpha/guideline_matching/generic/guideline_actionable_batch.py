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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import traceback
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


class GenericActionableBatch(DefaultBaseModel):
    guideline_id: str
    condition: str
    rationale: str
    applies: bool


class GenericActionableGuidelineMatchesSchema(DefaultBaseModel):
    checks: Sequence[GenericActionableBatch]


@dataclass
class GenericActionableGuidelineGuidelineMatchingShot(Shot):
    interaction_events: Sequence[Event]
    guidelines: Sequence[GuidelineContent]
    expected_result: GenericActionableGuidelineMatchesSchema


class GenericActionableGuidelineMatchingBatch(GuidelineMatchingBatch):
    def __init__(
        self,
        logger: Logger,
        meter: Meter,
        optimization_policy: OptimizationPolicy,
        schematic_generator: SchematicGenerator[GenericActionableGuidelineMatchesSchema],
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
                        if match.applies:
                            self._logger.debug(f"Activated:\n{match.model_dump_json(indent=2)}")

                            matches.append(
                                GuidelineMatch(
                                    guideline=self._guidelines[match.guideline_id],
                                    score=10 if match.applies else 1,
                                    rationale=match.rationale,
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

    async def shots(self) -> Sequence[GenericActionableGuidelineGuidelineMatchingShot]:
        return await shot_collection.list()

    def _format_shots(
        self, shots: Sequence[GenericActionableGuidelineGuidelineMatchingShot]
    ) -> str:
        return "\n".join(
            f"示例 #{i}：###\n{self._format_shot(shot)}" for i, shot in enumerate(shots, start=1)
        )

    def _format_shot(self, shot: GenericActionableGuidelineGuidelineMatchingShot) -> str:
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
                f"{i}) 条件：{g.condition}。动作：{g.action}"
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

    def _build_prompt(
        self,
        shots: Sequence[GenericActionableGuidelineGuidelineMatchingShot],
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
            name="actionable-guideline-general-instructions-task-description",
            template="""
总体说明
-----------------
本系统中，对话 AI 客服的行为由「指导原则」指导。客服在与用户（也称客户）交互时会使用这些指导原则。
每条指导原则由两部分组成：
-「条件」：用自然语言描述指导原则何时适用。我们根据对话当前状态检查该条件，以判断该指导原则是否应参与生成对用户的下一条回复。
-「动作」：当指导原则的「条件」在对话当前状态下成立时，客服应遵循的自然语言指令。此处描述仅针对客服，不针对用户。

任务说明
----------------
你的任务是评估一组给定的「何时」条件与你（AI 客服）和用户之间交互的最近状态的相关性与适用性。
你在假设该动作尚未在交互中执行的前提下，检查每条指导原则的适用性。

若指导原则与对话最新部分、尤其是最近一条客户消息相关，则标为适用。若话题已转换，不要仅根据对话较早部分将指导原则标为适用，即使之前话题未解决或该动作从未执行。

若对话从较宽泛的问题转向相关子问题（同一整体问题下的相关细节或后续），只要指导原则与该子问题相关，仍应视为适用，因其属于当前讨论的一部分。
反之，若对话已明显转向全新话题，则之前的指导原则不应标为适用。
这样既把适用性绑定到当前上下文，又保留在深入子话题时讨论的连续性。

在判断对话是转向相关子问题还是完全不同的话题时，考虑客户是否仍有意解决此前满足该条件的询问。
若客户仍在追求该原始询问，则当前讨论应视为其子问题。不必关心原问题是否已解决，只需判断当前问题是否为该条件的子问题。

回复的确切格式将在本提示后文给出。

""",
            props={},
        )
        builder.add_section(
            name="actionable-guideline-matcher-examples-of-evaluations",
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
            props={"guidelines_text": guidelines_text},
            status=SectionStatus.ACTIVE,
        )

        builder.add_section(
            name="actionable-guideline-output-format",
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
                    guideline_representations=guideline_representations
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
                "rationale": "<说明在关注最近交互时该条件为何满足或不满足>",
                "applies": "<BOOL>",
            }
            for i, g in self._guidelines.items()
        ]
        result = {"checks": result_structure}
        return json.dumps(result, indent=4)


class GenericActionableGuidelineMatching(GuidelineMatchingStrategy):
    def __init__(
        self,
        logger: Logger,
        meter: Meter,
        optimization_policy: OptimizationPolicy,
        entity_queries: EntityQueries,
        schematic_generator: SchematicGenerator[GenericActionableGuidelineMatchesSchema],
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
            hints={"type": GenericActionableGuidelineMatchingBatch},
        )

    def _create_batch(
        self,
        guidelines: Sequence[Guideline],
        journeys: Sequence[Journey],
        context: GuidelineMatchingContext,
    ) -> GenericActionableGuidelineMatchingBatch:
        return GenericActionableGuidelineMatchingBatch(
            logger=self._logger,
            meter=self._meter,
            optimization_policy=self._optimization_policy,
            schematic_generator=self._schematic_generator,
            guidelines=guidelines,
            journeys=journeys,
            context=context,
        )

    @override
    async def transform_matches(
        self,
        matches: Sequence[GuidelineMatch],
    ) -> Sequence[GuidelineMatch]:
        return matches


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
        "Hi, I'm planning a trip to Italy next month. What can I do there?",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "That sounds exciting! I can help you with that. Do you prefer exploring cities or enjoying scenic landscapes?",
    ),
    _make_event(
        "34",
        EventSource.CUSTOMER,
        "Can you help me figure out the best time to visit Rome and what to pack?",
    ),
    _make_event(
        "78",
        EventSource.CUSTOMER,
        "Actually I’m also wondering — do I need any special visas or documents as an American citizen?",
    ),
]

example_1_guidelines = [
    GuidelineContent(
        condition="The customer is looking for flight or accommodation booking assistance",
        action="Provide links or suggestions for flight aggregators and hotel booking platforms.",
    ),
    GuidelineContent(
        condition="The customer asks for activities recommendations",
        action="Guide them in refining their preferences and suggest options that match what they're looking for",
    ),
    GuidelineContent(
        condition="The customer asks for logistical or legal requirements.",
        action="Provide a clear answer or direct them to a trusted official source if uncertain.",
    ),
]

example_1_expected = GenericActionableGuidelineMatchesSchema(
    checks=[
        GenericActionableBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer is looking for flight or accommodation booking assistance",
            rationale="There’s no mention of booking logistics like flights or hotels",
            applies=False,
        ),
        GenericActionableBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer asks for activities recommendations",
            rationale="The customer has moved from seeking activity recommendations to asking about legal requirements. Since they are no longer pursuing their original inquiry about activities, this represents a new topic rather than a sub-issue",
            applies=False,
        ),
        GenericActionableBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer asks for logistical or legal requirements.",
            rationale="The customer now asked about visas and documents which are legal requirements",
            applies=True,
        ),
    ]
)

example_2_events = [
    _make_event(
        "21",
        EventSource.CUSTOMER,
        "Hi, I’m interested in your Python programming course, but I’m not sure if I’m ready for it.",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "Happy to help! Could you share a bit about your background or experience with programming so far?",
    ),
    _make_event(
        "32",
        EventSource.CUSTOMER,
        "I’ve done some HTML and CSS, but never written real code before.",
    ),
    _make_event(
        "48",
        EventSource.AI_AGENT,
        "Thanks for sharing! That gives me a good idea. Our Python course is beginner-friendly, but it does assume you're comfortable with logic and problem solving. Would you like me "
        "to recommend a short prep course first?",
    ),
    _make_event(
        "78",
        EventSource.CUSTOMER,
        "That sounds useful. But I'm also wondering — is the course self-paced? I work full time.",
    ),
]

example_2_guidelines = [
    GuidelineContent(
        condition="The customer mentions a constraint that is related to commitment to the course",
        action="Emphasize flexible learning options",
    ),
    GuidelineContent(
        condition="The user expresses hesitation or self-doubt.",
        action="Affirm that it's okay to be uncertain and provide confidence-building context",
    ),
    GuidelineContent(
        condition="The user asks about certification or course completion benefits.",
        action="Clearly explain what the user receives",
    ),
]

example_2_expected = GenericActionableGuidelineMatchesSchema(
    checks=[
        GenericActionableBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The customer mentions a constraint that is related to commitment to the course",
            rationale="In the most recent message, the customer mentions that they work full time which is a constraint",
            applies=True,
        ),
        GenericActionableBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The user expresses hesitation or self-doubt.",
            rationale="In the most recent message the user still sounds hesitant about their fit to the course",
            applies=True,
        ),
        GenericActionableBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="The user asks about certification or course completion benefits.",
            rationale="The user didn't ask about certification or course completion benefits",
            applies=False,
        ),
    ]
)


example_3_events = [
    _make_event(
        "21",
        EventSource.CUSTOMER,
        "I'm having trouble logging into my account.",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "I'm sorry to hear that. Can you tell me what happens when you try to log in?",
    ),
    _make_event(
        "27",
        EventSource.CUSTOMER,
        "It says my password is incorrect.",
    ),
    _make_event(
        "48",
        EventSource.AI_AGENT,
        "Have you tried resetting your password?",
    ),
    _make_event(
        "78",
        EventSource.CUSTOMER,
        "Yes, I did, but I can't access my mail to complete the reset.",
    ),
]

example_3_guidelines = [
    GuidelineContent(
        condition="When the user is having a problem with login.",
        action="Help them identify the problem and solve it",
    ),
]

example_3_expected = GenericActionableGuidelineMatchesSchema(
    checks=[
        GenericActionableBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="When the user is having a problem with login.",
            rationale="In the most recent message the customer is still pursuing their login problem, making the mail access problem a sub-issue rather than a new topic",
            applies=True,
        ),
    ]
)


example_4_events = [
    _make_event(
        "21",
        EventSource.CUSTOMER,
        "Hi, I'm thinking about ordering this coat, but I need to know — what's your return policy?",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "You can return items within 30 days either in-store or using our prepaid return label.",
    ),
    _make_event("27", EventSource.CUSTOMER, "And what happens if I’ve already worn it once?"),
]

example_4_guidelines = [
    GuidelineContent(
        condition="When the customer asks about how to return an item.",
        action="Mention both in-store and delivery service return options.",
    ),
]

example_4_expected = GenericActionableGuidelineMatchesSchema(
    checks=[
        GenericActionableBatch(
            guideline_id=GuidelineId("<example-id-for-few-shots--do-not-use-this-in-output>"),
            condition="When the customer asks about how to return an item.",
            rationale="In the most recent message the customer asks about what happens when they wore the item, which is an inquiry regarding returning an item",
            applies=True,
        ),
    ]
)


_baseline_shots: Sequence[GenericActionableGuidelineGuidelineMatchingShot] = [
    GenericActionableGuidelineGuidelineMatchingShot(
        description="",
        interaction_events=example_1_events,
        guidelines=example_1_guidelines,
        expected_result=example_1_expected,
    ),
    GenericActionableGuidelineGuidelineMatchingShot(
        description="",
        interaction_events=example_2_events,
        guidelines=example_2_guidelines,
        expected_result=example_2_expected,
    ),
    GenericActionableGuidelineGuidelineMatchingShot(
        description="",
        interaction_events=example_3_events,
        guidelines=example_3_guidelines,
        expected_result=example_3_expected,
    ),
    GenericActionableGuidelineGuidelineMatchingShot(
        description="",
        interaction_events=example_4_events,
        guidelines=example_4_guidelines,
        expected_result=example_4_expected,
    ),
]

shot_collection = ShotCollection[GenericActionableGuidelineGuidelineMatchingShot](_baseline_shots)
