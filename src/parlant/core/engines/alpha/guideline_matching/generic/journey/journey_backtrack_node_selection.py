from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import traceback
from typing import Any, Optional, cast
from parlant.core.common import Criticality, DefaultBaseModel, JSONSerializable
from parlant.core.engines.alpha.guideline_matching.generic.common import internal_representation
from parlant.core.engines.alpha.guideline_matching.guideline_match import GuidelineMatch
from parlant.core.engines.alpha.guideline_matching.guideline_matcher import (
    GuidelineMatchingBatchError,
    GuidelineMatchingBatchResult,
)
from parlant.core.engines.alpha.guideline_matching.guideline_matching_context import (
    GuidelineMatchingContext,
)
from parlant.core.engines.alpha.optimization_policy import OptimizationPolicy
from parlant.core.engines.alpha.prompt_builder import PromptBuilder
from parlant.core.guidelines import Guideline, GuidelineContent, GuidelineId, GuidelineStore
from parlant.core.journeys import Journey
from parlant.core.loggers import Logger
from parlant.core.nlp.generation import SchematicGenerator
from parlant.core.sessions import Event, EventId, EventKind, EventSource
from parlant.core.shots import Shot, ShotCollection


PRE_ROOT_INDEX = "0"
ROOT_INDEX = "1"

DEFAULT_ROOT_ACTION = (
    "<<旅程起点：根据上下文在适当的步骤开始旅程>>"
)
BEGIN_JOURNEY_AT_ACTIONLESS_ROOT_FLAG_TEXT = "- 从这里开始：在此步骤开始旅程推进。根据相关转换推进到下一个节点。"
BEGIN_JOURNEY_AT_ROOT_WITH_ACTION_FLAG_TEXT = "- 从这里开始：在此步骤开始旅程推进。如果此步骤已完成，则继续推进。"
EXIT_JOURNEY_INSTRUCTION = "返回 'NONE'"
ELSE_CONDITION_STR = "此步骤已完成，且没有其他转换适用"
SINGLE_FOLLOW_UP_CONDITION_STR = "此步骤已完成"
FORK_NODE_ACTION_STR = (
    "无需操作 - 始终根据相关转换推进到下一步"
)
LAST_PRESENTED_NODE_INSTRUCTION = "不要越过此步骤。如果你到了这里 - 将此步骤标记为未完成并将其作为 next_step 返回"



class JourneyNodeKind(Enum):
    FORK = "fork"
    CHAT = "chat"
    TOOL = "tool"
    NA = "NA"


class StepCompletionStatus(Enum):
    COMPLETED = "completed"
    NEEDS_CUSTOMER_INPUT = "needs_customer_input"
    NEEDS_AGENT_ACTION = "needs_agent_action"
    NEEDS_TOOL_CALL = "needs_tool_call"


@dataclass
class _JourneyEdge:
    target_guideline: Guideline | None
    condition: str | None
    source_node_index: str
    target_node_index: str


@dataclass
class _JourneyNode:  # Refactor after node type is implemented
    id: str
    action: str | None
    incoming_edges: list[_JourneyEdge]
    outgoing_edges: list[_JourneyEdge]
    kind: JourneyNodeKind
    customer_dependent_action: bool
    customer_action_description: Optional[str] = None
    agent_dependent_action: Optional[bool] = None
    agent_action_description: Optional[str] = None


class JourneyNodeAdvancement(DefaultBaseModel):
    id: str
    completed: StepCompletionStatus
    follow_ups: Optional[list[str]] = None


class JourneyBacktrackNodeSelectionSchema(DefaultBaseModel):
    rationale: str | None = None
    journey_applies: bool | None = None
    requires_backtracking: bool | None = None
    backtracking_target_step: str | None = None
    step_advancement: Sequence[JourneyNodeAdvancement] | None = None
    next_step: str | None = None


@dataclass
class JourneyNodeSelectionShot(Shot):
    interaction_events: Sequence[Event]
    journey_title: str
    journey_nodes: dict[str, _JourneyNode] | None
    previous_path: Sequence[str | None]
    expected_result: JourneyBacktrackNodeSelectionSchema
    conditions: Sequence[str]


def build_node_wrappers(guidelines: Sequence[Guideline]) -> dict[str, _JourneyNode]:
    def _get_guideline_node_index(guideline: Guideline) -> str:
        return str(
            cast(dict[str, JSONSerializable], guideline.metadata["journey_node"]).get(
                "index", "-1"
            ),
        )

    guideline_id_to_guideline: dict[GuidelineId, Guideline] = {g.id: g for g in guidelines}
    guideline_id_to_node_index: dict[GuidelineId, str] = {
        g.id: _get_guideline_node_index(g) for g in guidelines
    }
    node_wrappers: dict[str, _JourneyNode] = {}

    # Build nodes
    for g in guidelines:
        node_index: str = guideline_id_to_node_index[g.id]
        if node_index not in node_wrappers:
            kind = JourneyNodeKind(
                cast(dict[str, Any], g.metadata.get("journey_node", {})).get("kind", "NA")
            )
            customer_dependent_action = cast(
                dict[str, bool], g.metadata.get("customer_dependent_action_data", {})
            ).get("is_customer_dependent", False)
            node_wrappers[node_index] = _JourneyNode(
                id=_get_guideline_node_index(g),
                action=FORK_NODE_ACTION_STR
                if kind == JourneyNodeKind.FORK
                else internal_representation(g).action,
                incoming_edges=[],
                outgoing_edges=[],
                kind=kind,
                customer_dependent_action=customer_dependent_action,
                customer_action_description=cast(
                    dict[str, str | None], g.metadata.get("customer_dependent_action_data", {})
                ).get("customer_action", None),
                agent_dependent_action=cast(
                    dict[str, bool], g.metadata.get("customer_dependent_action_data", {})
                ).get(
                    "is_agent_dependent",
                    not customer_dependent_action and kind == JourneyNodeKind.CHAT,
                ),
                agent_action_description=cast(
                    dict[str, str | None], g.metadata.get("customer_dependent_action_data", {})
                ).get("agent_action", None),
            )

    # Build edges
    registered_edges: set[tuple[str, str]] = set()
    for g in guidelines:
        source_node_index: str = guideline_id_to_node_index[g.id]
        for followup_id in cast(
            dict[str, Sequence[GuidelineId]], g.metadata.get("journey_node", {})
        ).get("follow_ups", []):
            followup_node_index: str = guideline_id_to_node_index[GuidelineId(followup_id)]
            followup_guideline = next((g for g in guidelines if g.id == followup_id), None)
            if (
                followup_guideline
                and (source_node_index, followup_node_index) not in registered_edges
            ):
                edge = _JourneyEdge(
                    target_guideline=guideline_id_to_guideline[followup_id],
                    condition=guideline_id_to_guideline[followup_id].content.condition,
                    source_node_index=source_node_index,
                    target_node_index=followup_node_index,
                )
                node_wrappers[source_node_index].outgoing_edges.append(edge)
                node_wrappers[followup_node_index].incoming_edges.append(edge)
                registered_edges.add((source_node_index, followup_node_index))
    if (
        ROOT_INDEX in node_wrappers
        and node_wrappers[ROOT_INDEX].action
        and len(node_wrappers[ROOT_INDEX].incoming_edges) == 0
    ):
        node_wrappers[ROOT_INDEX].incoming_edges.append(
            _JourneyEdge(
                target_guideline=next(
                    g for g in guidelines if _get_guideline_node_index(g) == ROOT_INDEX
                ),
                condition=None,
                source_node_index=PRE_ROOT_INDEX,
                target_node_index=ROOT_INDEX,
            )
        )

    return node_wrappers


def get_pruned_nodes(
    nodes: dict[str, _JourneyNode],
    previous_path: Sequence[str | None],
    max_depth: int,
) -> dict[str, _JourneyNode]:
    # TODO can be implemented in cleaner fashion if we maintain a dictionary of the distance of each node from the previous path / current node
    # If we encounter any trouble with pruning - we should implement it as such
    if previous_path and set(previous_path) != set([None]):
        nodes_to_traverse = set(previous_path)
    else:
        nodes_to_traverse = set("1")

    visited: set[str | None] = set()
    result: set[str | None] = set()

    queue: deque[tuple[str | None, int]] = deque()

    for node in nodes_to_traverse:
        visited = set()
        queue.append((node, 0))
        while queue:
            current, depth = queue.popleft()
            if not current:
                continue

            if depth > max_depth or current in visited:
                continue

            visited.add(current)
            result.add(current)

            # If node run tools, no need to show the steps further.
            if nodes[current].kind == JourneyNodeKind.TOOL and (
                not previous_path or current not in previous_path
            ):
                continue

            for edge in nodes[current].outgoing_edges:
                neighbor = edge.target_node_index
                queue.append((neighbor, depth + 1))

    pruned_nodes = {idx: nodes[idx] for idx in result if idx}
    if not pruned_nodes:  # Recover in case some unexpected error caused all nodes to be pruned
        return get_pruned_nodes(nodes, [], max_depth)
    return pruned_nodes


def get_journey_transition_map_text(
    nodes: dict[str, _JourneyNode],
    journey_title: str,
    journey_description: str = "",
    journey_conditions: Sequence[Guideline] = [],
    previous_path: Sequence[str | None] = [],
    print_customer_action_description: bool = False,
    to_prune: bool = False,
    max_depth: int = 5,
) -> str:
    def node_sort_key(node_index: str) -> Any:
        try:
            return int(node_index)
        except Exception:
            return node_index

    def get_node_transition_text(node: _JourneyNode) -> str:
        result = ""
        if len(node.outgoing_edges) == 0:
            result = f"""↳ 若「此步骤已完成」→ {EXIT_JOURNEY_INSTRUCTION}"""
        elif len(node.outgoing_edges) == 1:
            if (
                to_prune
                and nodes[node.outgoing_edges[0].target_node_index].action
                and node.outgoing_edges[0].target_node_index in nodes
                and node.outgoing_edges[0].target_node_index not in unpruned_nodes
            ):
                result = LAST_PRESENTED_NODE_INSTRUCTION
            else:
                followup_instruction = (
                    f"转到步骤 {node.outgoing_edges[0].target_node_index}"
                    if (
                        node.outgoing_edges[0].target_node_index in nodes
                        and nodes[node.outgoing_edges[0].target_node_index].action
                    )
                    else EXIT_JOURNEY_INSTRUCTION
                )
                result = f"""↳ 若「{node.outgoing_edges[0].condition or SINGLE_FOLLOW_UP_CONDITION_STR}」→ {followup_instruction}"""
        else:
            if to_prune and any(
                e.target_node_index not in unpruned_nodes
                for e in node.outgoing_edges
                if nodes[node.outgoing_edges[0].target_node_index].action
            ):
                result = LAST_PRESENTED_NODE_INSTRUCTION
            else:
                result = "\n".join(
                    [
                        f"""↳ 若「{e.condition or ELSE_CONDITION_STR}」→ {
                            f"转到步骤 {e.target_node_index}"
                            if e.target_node_index in nodes and nodes[e.target_node_index].action
                            else EXIT_JOURNEY_INSTRUCTION
                        }"""
                        for e in node.outgoing_edges
                    ]
                )
        return result

    unpruned_nodes = (
        get_pruned_nodes(
            nodes,
            previous_path,
            max_depth,
        )
        if to_prune
        else nodes
    )

    if journey_description:
        journey_description_str = f"\n旅程描述：{journey_description}"
    else:
        journey_description_str = ""
    if journey_conditions:
        journey_conditions_str = " 或 ".join(f'"{g.content.condition}"' for g in journey_conditions)
        journey_conditions_str = f"\n旅程激活条件：{journey_conditions_str}"
    else:
        journey_conditions_str = ""

    last_executed_node_id = next(
        (node_id for node_id in reversed(previous_path) if node_id is not None), None
    )
    first_node_to_execute: str | None = None
    nodes_str = ""
    for node_index in sorted(unpruned_nodes.keys(), key=node_sort_key):
        displayed_node_action = ""

        node: _JourneyNode = nodes[node_index]
        print_node = True
        flags_str = "步骤标志：\n"
        if node.id == ROOT_INDEX:
            if (
                node.action and node.action != DEFAULT_ROOT_ACTION
            ):  # Root with real action, so we must print it
                if not previous_path or set(previous_path) == set([None]):
                    flags_str += BEGIN_JOURNEY_AT_ROOT_WITH_ACTION_FLAG_TEXT + "\n"
                displayed_node_action = node.action
            elif (
                len(node.outgoing_edges) > 1
            ):  # Root has no real action but has multiple followups, so should be printed
                if not previous_path or set(previous_path) == set([None]):
                    flags_str += BEGIN_JOURNEY_AT_ACTIONLESS_ROOT_FLAG_TEXT + "\n"
                displayed_node_action = FORK_NODE_ACTION_STR
            else:  # Root has no action and a single follow up, so that follow up is first to be executed
                print_node = False
                if not previous_path or set(previous_path) == set([None]):
                    first_node_to_execute = node.outgoing_edges[0].target_node_index
        # Customer / Agent dependent flags
        if node.customer_dependent_action:
            if print_customer_action_description and node.customer_action_description:
                flags_str += f'- 依赖客户操作：本步骤需客户完成相应操作后才视为完成。若以下操作已完成则视为完成：「{node.customer_action_description}」\n'
            else:
                flags_str += "- 依赖客户操作：本步骤需客户完成相应操作后才视为完成。若客户已回答步骤中的问题，则将其标记为完成。\n"
        if node.kind == JourneyNodeKind.CHAT and node.agent_dependent_action:
            flags_str += "- 需客服执行：本步骤可能需客服发言或执行动作后才完成。仅当客服已完成所述操作后再推进。\n"

        # Node kind flags
        if (
            node.kind in {JourneyNodeKind.CHAT, JourneyNodeKind.NA}
            and node.action is None
            and len(node.outgoing_edges) <= 1
        ):
            print_node = False
        elif node.kind == JourneyNodeKind.FORK or displayed_node_action == FORK_NODE_ACTION_STR:
            displayed_node_action = FORK_NODE_ACTION_STR
            flags_str += "- 切勿将本步骤作为 NEXT_STEP 输出：本步骤为过渡步骤，不得作为 next_step 返回。请始终从其继续推进。\n"
        else:
            displayed_node_action = cast(str, node.action)
        if node.kind == JourneyNodeKind.TOOL and node.id != last_executed_node_id:
            flags_str += (
                "- 需调用工具：不要越过此步骤！若已到达此处，请停止推进。\n"
            )

        # Previously executed-related flags
        if node.id == first_node_to_execute:
            flags_str += BEGIN_JOURNEY_AT_ROOT_WITH_ACTION_FLAG_TEXT
        elif node.id == last_executed_node_id:
            flags_str += (
                "- 这是上次执行的步骤。请从此步骤开始继续推进。\n"
            )
        elif node.id in previous_path:
            flags_str += "- 已执行过：本步骤此前已执行。可以回退到此步骤。\n"
        elif node.id != ROOT_INDEX:
            flags_str += "- 未执行过：本步骤此前未执行。不可回退到此步骤。\n"
        if print_node:
            nodes_str += f"""
步骤 {node_index}：{displayed_node_action}
{flags_str}
转换规则：
{get_node_transition_text(node)}
"""
    return f"""
旅程：{journey_title}
{journey_conditions_str}{journey_description_str}

步骤列表：
{nodes_str}
"""


class JourneyBacktrackNodeSelection:
    def __init__(
        self,
        logger: Logger,
        guideline_store: GuidelineStore,
        optimization_policy: OptimizationPolicy,
        schematic_generator: SchematicGenerator[JourneyBacktrackNodeSelectionSchema],
        examined_journey: Journey,
        context: GuidelineMatchingContext,
        node_guidelines: Sequence[Guideline] = [],
        journey_path: Sequence[str | None] = [],
        journey_conditions: Sequence[Guideline] = [],
    ) -> None:
        self._logger = logger

        self._guideline_store = guideline_store

        self._optimization_policy = optimization_policy
        self._schematic_generator = schematic_generator
        self._node_wrappers: dict[str, _JourneyNode] = build_node_wrappers(node_guidelines)
        self._root_guideline = self._get_root(node_guidelines)
        self._context = context
        self._examined_journey = examined_journey
        self._previous_path: Sequence[str | None] = journey_path
        self._journey_conditions = journey_conditions

    def _get_root(self, node_guidelines: Sequence[Guideline]) -> Guideline:
        def _get_guideline_node_index(guideline: Guideline) -> str:
            return str(
                cast(dict[str, JSONSerializable], guideline.metadata["journey_node"]).get(
                    "index", "-1"
                ),
            )

        return next(g for g in node_guidelines if _get_guideline_node_index(g) == ROOT_INDEX)

    async def process(self) -> GuidelineMatchingBatchResult:
        prompt = self._build_prompt(shots=await self.shots())

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
                    f"Completion: {self._examined_journey.title}\n{inference.content.model_dump_json(indent=2)}"
                )

                journey_path = self._get_verified_node_advancement(inference.content)

                # Get correct guideline to return based on the transition into next_step  TODO consider surrounding with try catch specifically
                matched_guideline: Guideline | None = None
                if inference.content.next_step in self._node_wrappers:
                    if len(journey_path) > 1 and [
                        e
                        for e in self._node_wrappers[inference.content.next_step].incoming_edges
                        if e.source_node_index == journey_path[-2]
                    ]:
                        matched_guideline = next(
                            e
                            for e in self._node_wrappers[inference.content.next_step].incoming_edges
                            if e.source_node_index == journey_path[-2]
                        ).target_guideline
                    else:
                        matched_guideline = (
                            self._node_wrappers[inference.content.next_step]
                            .incoming_edges[0]
                            .target_guideline
                        )
                return GuidelineMatchingBatchResult(
                    matches=[
                        GuidelineMatch(
                            guideline=matched_guideline,
                            score=10,
                            rationale=f"该指导原则作为「旅程」（按顺序执行的一系列动作）的一部分被选中。可据此理解对话如何到达当前节点。选择本步骤的理由：{inference.content.rationale}",
                            metadata={
                                "journey_path": list(self._previous_path) + journey_path,
                                "step_selection_journey_id": self._examined_journey.id,
                            },
                        )
                    ]
                    if matched_guideline  # If either 'None' or an illegal step was returned, return root guideline, a place holder for "exit journey"
                    else [
                        GuidelineMatch(
                            guideline=self._root_guideline,
                            score=10,
                            rationale=f"已选择根指导原则，表示应退出旅程。选择理由：{inference.content.rationale}",
                            metadata={
                                "journey_path": list(self._previous_path) + journey_path + [None],
                                "step_selection_journey_id": self._examined_journey.id,
                            },
                        )
                    ],
                    generation_info=inference.info,
                )
            except Exception as exc:
                self._logger.warning(
                    f"Attempt {generation_attempt} failed: {self._examined_journey.title}\n{traceback.format_exception(exc)}"
                )

                last_generation_exception = exc

        raise GuidelineMatchingBatchError() from last_generation_exception

    async def shots(self) -> Sequence[JourneyNodeSelectionShot]:
        return await shot_collection.list()

    def _format_shots(self, shots: Sequence[JourneyNodeSelectionShot]) -> str:
        return "\n".join(
            f"示例 #{i}：{shot.journey_title}\n{self._format_shot(shot)}"
            for i, shot in enumerate(shots, start=1)
        )

    def _format_shot(self, shot: JourneyNodeSelectionShot) -> str:
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
        if shot.journey_nodes:
            formatted_shot += get_journey_transition_map_text(
                shot.journey_nodes,
                previous_path=shot.previous_path,
                journey_title=shot.journey_title,
                journey_conditions=[
                    Guideline(
                        id=GuidelineId(f"c-{i}"),
                        creation_utc=datetime.now(timezone.utc),
                        metadata={"journey_node": {"journey_id": "journey"}},
                        content=GuidelineContent(
                            condition=c,
                            action=None,
                        ),
                        enabled=False,
                        criticality=Criticality.HIGH,
                        tags=[],
                    )
                    for i, c in enumerate(shot.conditions)
                ],
                print_customer_action_description=True,
            )

        formatted_shot += f"""
- **期望结果**：
```json
{json.dumps(shot.expected_result.model_dump(mode="json", exclude_unset=True), indent=2)}
```
"""
        return formatted_shot

    def _get_verified_node_advancement(
        self, response: JourneyBacktrackNodeSelectionSchema
    ) -> list[str | None]:
        def add_and_remove_list_values(
            list_to_alter: list[Any],
            indexes_to_add: Sequence[tuple[int, Any]],
            indexes_to_delete: Sequence[int],
        ) -> list[Any]:
            result = list_to_alter.copy()

            for i in reversed(indexes_to_delete):
                del result[i]

            for original_i, value in indexes_to_add:
                deletions_before = sum(1 for del_i in indexes_to_delete if del_i < original_i)
                additions_before = sum(1 for add_i, _ in indexes_to_add if add_i < original_i)
                adjusted_i = original_i - deletions_before + additions_before
                result.insert(adjusted_i, value)

            return result

        journey_path: list[str | None] = []
        for i, advancement in enumerate(response.step_advancement or []):
            journey_path.append(advancement.id)
            if (
                i > 0
                and advancement.id in self._node_wrappers
                and self._node_wrappers[advancement.id].kind == JourneyNodeKind.TOOL
            ):
                break  # Don't continue past tool calling step

        if (
            response.requires_backtracking and journey_path
        ):  # Warnings related to backtracking to illegal step
            if journey_path[0] != response.backtracking_target_step:
                self._logger.warning(
                    f"WARNING: Illegal journey path returned by journey step selection for journey {self._examined_journey.title}. Reported that it should return to step {response.backtracking_target_step}, but step advancement began at {journey_path[0]}"
                )
            if response.backtracking_target_step not in self._previous_path:
                self._logger.warning(
                    f"WARNING: Illegal journey path returned by journey step selection for journey {self._examined_journey.title}. Backtracked to {response.backtracking_target_step}, which was never previously visited! Previously visited step IDs: {self._previous_path}"
                )
        elif (
            self._previous_path
            and self._previous_path[-1]
            and journey_path
            and journey_path[0] != self._previous_path[-1]
        ):  # Illegal first step returned
            self._logger.warning(
                f"WARNING: Illegal journey path returned by journey step selection for journey {self._examined_journey.title}. Expected path from {self._previous_path} to {journey_path}"
            )
            journey_path.insert(0, self._previous_path[-1])  # Try to recover

        indexes_to_delete: list[int] = []
        indexes_to_add: list[tuple[int, str]] = []
        for i in range(1, len(journey_path)):  # Verify all transitions are legal
            if journey_path[i - 1] not in self._node_wrappers:
                self._logger.warning(
                    f"WARNING: Illegal journey path returned by journey step selection for journey {self._examined_journey.title}. Illegal step returned: {journey_path[i - 1]}. Full path: : {journey_path}"
                )
                indexes_to_delete.append(i)
            elif journey_path[i] not in [
                e.target_node_index
                for e in self._node_wrappers[cast(str, journey_path[i - 1])].outgoing_edges
            ]:
                self._logger.warning(
                    f"WARNING: Illegal transition in journey path returned by journey step selection for journey {self._examined_journey.title} - from {journey_path[i - 1]} to {journey_path[i]}. Full path: : {journey_path}"
                )
                # Sometimes, the LLM returns a path that would've been legal if it were not for an out-of-place step. This deletes such steps.
                if i + 1 < len(journey_path) and journey_path[i + 1] in [
                    e.target_node_index
                    for e in self._node_wrappers[str(journey_path[i - 1])].outgoing_edges
                ]:
                    indexes_to_delete.append(i)
                else:
                    # In other cases, it skips a node that would make the path valid. We want to identify and add the missing node
                    previous_node_follow_ups = set(
                        e.target_node_index
                        for e in self._node_wrappers[cast(str, journey_path[i - 1])].outgoing_edges
                        if e.source_node_index == journey_path[i - 1]
                    )
                    if journey_path[i] in self._node_wrappers:
                        current_node_origins = set(
                            e.source_node_index
                            for e in self._node_wrappers[cast(str, journey_path[i])].incoming_edges
                        )

                        possible_connector_nodes: list[str] = list(
                            previous_node_follow_ups.intersection(current_node_origins)
                        )
                    else:
                        possible_connector_nodes = list(previous_node_follow_ups)
                    if len(possible_connector_nodes) == 1:
                        indexes_to_add.append((i, possible_connector_nodes[0]))
        journey_path = add_and_remove_list_values(journey_path, indexes_to_add, indexes_to_delete)

        if (
            journey_path and journey_path[-1] not in self._node_wrappers
        ):  # 'Exit journey' was selected, or illegal value returned (both should cause no guidelines to be active)
            journey_path[-1] = None

        return journey_path

    def _build_prompt(
        self,
        shots: Sequence[JourneyNodeSelectionShot],
    ) -> PromptBuilder:
        builder = PromptBuilder(
            on_build=lambda prompt: self._logger.trace(
                f"Prompt: {self._examined_journey.title}\n{prompt}"
            )
        )

        builder.add_agent_identity(self._context.agent)

        builder.add_section(
            name="journey-step-selection-general-instructions",
            template="""
总体说明
-------------------
你是一名名为 {agent_name} 的 AI 客服，代表企业与客户进行多轮对话。
对话围绕预定义的「旅程」展开——即引导客户对话达成特定结果的系统化流程。

## 旅程结构
每个旅程包含：
- **步骤**：你需要执行的单个动作（如提问、提供信息、执行任务）
- **转换**：根据客户回复或完成状态决定下一步的规则
- **标志**：影响步骤行为的特殊属性

## 核心任务
根据当前对话状态、以及上一步已执行的步骤，分析并确定下一个合适的旅程步骤。
""",
            props={"agent_name": self._context.agent.name},
        )
        builder.add_section(
            name="journey-step-selection-task_description",
            template="""
任务说明
-------------------
按以下流程确定下一个旅程步骤，并在指定输出格式中记录每次判断。

## 1：旅程上下文检查
判断对话是否应继续处于当前旅程中。
旅程一旦开始，应持续跟随，除非客户明确表示不再追求该旅程的目标。

除非客户明确要求离开该话题或完全放弃旅程目标，否则将 journey_applies 设为 true。
旅程条件仅用于首次激活——一旦激活，即使某些步骤与初始条件看似无关也继续执行。
当客户在回答问题、参与旅程流程或提供前序步骤所需信息时，旅程仍然适用，即使回复与初始条件略有偏离。
仅当客户明确表示要退出时（如「我不想再重置密码了」或「我们聊点别的吧」）才将 journey_applies 设为 false。
若 journey_applies 为 false，将 next_step 设为 'None' 并跳过后续步骤。

重要：若你已在执行旅程步骤（即存在「上一步」），旅程几乎总是继续。激活条件仅用于启动新旅程，不用于校验进行中的旅程。

## 2：回退检查
检查客户是否改变了先前决定，从而需要回到更早的步骤。
- 若客户否定或改变了之前的选择，将 `requires_backtracking` 设为 `true`
- 若需要回退：
  - 将 backtracking_target_step 设为客户改变决定时所在的步骤，该步骤必须带有「已执行过」标志
  - 继续执行第 4 步（旅程推进），但以 backtracking_target_step 为起点，而非 last_step
  - 推进应从回退目标步骤开始，按正常推进规则继续，直到遇到无法完成的步骤

## 3：当前步骤完成情况
评估上一步是否已完成：
- 对于「依赖客户操作」步骤：客户已提供所需信息（在被询问后或在此前消息中主动提供）。若是，将 completed 设为 'completed'；否则设为 'needs_customer_input'，且不要越过该步骤。
- 对于「需客服执行」步骤：客服已完成所需的沟通或动作。若是，将 completed 设为 'completed'；否则设为 'needs_agent_action'，且不要越过该步骤。
- 对于「需调用工具」步骤：该步骤需执行工具调用才算完成。若你从该步骤开始推进，且工具已执行，则将其标为完成并继续；否则始终将 completed 设为 false 并将其作为 next_step 返回。
- 若上一步未完成，将 next_step 设为当前步骤 ID（重复该步骤），并在 step_advancement 数组中记录。

## 4：旅程推进
从上一步开始，沿后续步骤推进，在 step_advancement 数组中记录每个步骤的完成状态。
仅当当前步骤已标为完成时才推进到下一步。
在每个已完成的步骤处，根据「转换规则」仔细判断后续步骤，仅推进到条件满足的那一步。
推进决策必须严格依据这些转换及条件——不得跳到条件未满足的步骤，即使你认为逻辑上应执行下一步。
取悦客户不能作为违反转换规则的理由，必须按条件推进到下一步。

在 step_advancement 中以前进路径形式记录，即从 last_step 到待执行下一步的步骤推进对象列表。每一步都必须是上一步的合法后续，且仅在上一步已完成时才能推进。

持续推进直到遇到以下情况之一：
- 需要调用工具的步骤（「需调用工具」标志）
- 缺少必要信息无法继续的步骤
- 需要向客户传达新内容（而非仅索要信息）的步骤（「需客服执行」标志）

**关于退出旅程**：
- "None" 为合法步骤 ID，表示「退出旅程」
- 对具有「退出旅程」转换的步骤，在 follow_ups 数组中包含 "None"
- 当旅程应退出时（因转换或已脱离旅程上下文），将 next_step 设为 "None"
""",
        )
        builder.add_section(
            name="journey-step-selection-examples",
            template="""
旅程步骤选择示例：
-------------------
{formatted_shots}

###
示例部分结束。以下为你需要据此做出判断的真实数据。
""",
            props={
                "formatted_shots": self._format_shots(shots),
                "shots": shots,
            },
        )

        builder.add_customer_identity(self._context.customer, self._context.session)
        builder.add_context_variables(self._context.context_variables)
        builder.add_glossary(self._context.terms)
        builder.add_capabilities_for_guideline_matching(self._context.capabilities)
        builder.add_interaction_history(self._context.interaction_history)
        builder.add_staged_tool_events(self._context.staged_events)

        builder.add_section(
            name="journey_description_background",
            template="以下是你当前所处的旅程。请仔细阅读并理清各步骤的先后关系：",
        )
        builder.add_section(
            name="journey-step-selection-journey-steps",
            template=get_journey_transition_map_text(
                nodes=self._node_wrappers,
                journey_title=self._examined_journey.title,
                previous_path=self._previous_path,
                journey_conditions=self._journey_conditions,
                journey_description=self._examined_journey.description,
                print_customer_action_description=True,
                to_prune=True,
            ),
        )
        builder.add_section(
            name="journey-step-selection-output-format",
            template="""{output_format}""",
            props={"output_format": self._get_output_format_section()},
        )

        builder.add_section(
            name="journey-general_reminder-section",
            template="""提醒：请认真遵守所有约束与说明。你必须完成本任务，否则可能对客户或所代表的企业造成损害。""",
        )

        return builder

    def _get_output_format_section(self) -> str:
        last_node = self._previous_path[-1] if self._previous_path else "None"
        return f"""
重要：请按以下 JSON 格式作答。

输出格式
-----------------
- 按说明填写下列字段。除非另有说明，否则均为必填。

```json
{{
  "rationale": "<str，说明下一步是什么以及为何选择该步骤>",
  "journey_applies": <bool，旅程是否应继续。提醒：若你已在执行旅程步骤（即存在「上一步」），旅程几乎总是继续。激活条件仅用于启动新旅程，不用于校验进行中的旅程。>,
  "requires_backtracking": <bool，是否需要回退到之前的步骤？>,
  "backtracking_target_step": "<str，客户改变决定时所在步骤的 id。若 requires_backtracking 为 false 则省略此字段>",
  "step_advancement": [
    {{
        "id": "<str，步骤 id。第一项应为 {last_node} 或 backtracking_target_step（若存在）>",
        "completed": <str，取 'completed'、'needs_customer_input'、'needs_agent_action' 或 'needs_tool_call' 之一>,
        "follow_ups": "<list[str]，本步骤的合法后续步骤 id 列表。若 completed 非 'completed' 则省略>"
    }},
    ... <按需添加更多步骤推进项>
  ],
  "next_step": "<str，要执行的下一步的 id，若旅程不应继续则为 'None'。必须与 step_advancement 中最后一项一致>"
}}
```
"""


def _make_event(e_id: str, source: EventSource, message: str) -> Event:
    return Event(
        id=EventId(e_id),
        source=source,
        kind=EventKind.MESSAGE,
        creation_utc=datetime.now(timezone.utc),
        offset=0,
        trace_id="",
        data={"message": message},
        deleted=False,
        metadata={},
    )


example_1_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "你好，我下个月打算去意大利。那边有什么好玩的？",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "听起来很棒！我可以帮你。你更喜欢逛城市还是看自然风光？",
    ),
    _make_event(
        "78",
        EventSource.CUSTOMER,
        "Actually I’m also wondering — 对了，作为美国公民我需要办签证或带什么证件吗？",
    ),
]


example_1_journey_nodes = {
    "1": _JourneyNode(
        id="1",
        kind=JourneyNodeKind.CHAT,
        action="询问客户更喜欢逛城市还是欣赏自然风光。",
        incoming_edges=[],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,  # Would need actual guidelines
                condition="客户更喜欢逛城市",
                source_node_index="1",
                target_node_index="2",
            ),
            _JourneyEdge(
                target_guideline=None,  # Would need actual guidelines
                condition="客户更喜欢自然风光",
                source_node_index="1",
                target_node_index="3",
            ),
            _JourneyEdge(
                target_guideline=None,  # Would need actual guidelines
                condition="客户提出与逛城市或自然风光无关的问题",
                source_node_index="1",
                target_node_index="4",
            ),
        ],
        customer_dependent_action=True,
        customer_action_description="客户已就城市与自然风光偏好做出回应",
    ),
    "2": _JourneyNode(
        id="2",
        kind=JourneyNodeKind.CHAT,
        action="推荐其意向国家的首都城市",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,  # Would need actual guidelines
                condition="客户更喜欢逛城市",
                source_node_index="1",
                target_node_index="2",
            )
        ],
        outgoing_edges=[],
        customer_dependent_action=False,
    ),
    "3": _JourneyNode(
        id="3",
        kind=JourneyNodeKind.CHAT,
        action="推荐其意向国家的热门徒步路线",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,  # Would need actual guidelines
                condition="客户更喜欢自然风光",
                source_node_index="1",
                target_node_index="3",
            )
        ],
        outgoing_edges=[],
        customer_dependent_action=False,
    ),
    "4": _JourneyNode(
        id="4",
        action="引导其查看旅行信息页",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,  # Would need actual guidelines
                condition="客户提出与逛城市或自然风光无关的问题",
                source_node_index="1",
                target_node_index="4",
            )
        ],
        outgoing_edges=[],
        customer_dependent_action=False,
        kind=JourneyNodeKind.CHAT,
    ),
}


example_1_expected = JourneyBacktrackNodeSelectionSchema(
    journey_applies=True,
    requires_backtracking=False,
    rationale="上一步已完成。客户询问签证事宜，与逛城市无关，因此应激活步骤 4",
    step_advancement=[
        JourneyNodeAdvancement(
            id="1", completed=StepCompletionStatus.COMPLETED, follow_ups=["2", "3", "4"]
        ),
        JourneyNodeAdvancement(id="4", completed=StepCompletionStatus.NEEDS_AGENT_ACTION),
    ],
    next_step="4",
)

example_2_events = [
    _make_event(
        "11",
        EventSource.AI_AGENT,
        "欢迎使用我们的出租车服务！请问今天需要什么帮助？",
    ),
    _make_event(
        "12",
        EventSource.CUSTOMER,
        "我想预约一辆出租车。",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "请问您从哪里上车？",
    ),
    _make_event(
        "34",
        EventSource.CUSTOMER,
        "我想从纽约 34 街西 20 号到肯尼迪机场，下午 5 点，现金支付。",
    ),
]

book_taxi_shot_journey_nodes = {
    "1": _JourneyNode(
        id="1",
        action="欢迎客户使用出租车服务",
        incoming_edges=[],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="你已欢迎客户",
                source_node_index="1",
                target_node_index="2",
            )
        ],
        customer_dependent_action=False,
        kind=JourneyNodeKind.CHAT,
    ),
    "2": _JourneyNode(
        id="2",
        action="询问客户上车地点",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="你已欢迎客户",
                source_node_index="1",
                target_node_index="2",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="上车地点在纽约市内",
                source_node_index="2",
                target_node_index="3",
            ),
            _JourneyEdge(
                target_guideline=None,
                condition="上车地点在纽约市外",
                source_node_index="2",
                target_node_index="4",
            ),
        ],
        customer_dependent_action=True,
        customer_action_description="客户已提供上车地点",
        kind=JourneyNodeKind.CHAT,
    ),
    "3": _JourneyNode(
        id="3",
        action="询问目的地",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="上车地点在纽约市内",
                source_node_index="2",
                target_node_index="3",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition=None,
                source_node_index="3",
                target_node_index="5",
            )
        ],
        customer_dependent_action=True,
        customer_action_description="客户已提供目的地",
        kind=JourneyNodeKind.CHAT,
    ),
    "4": _JourneyNode(
        id="4",
        action="告知客户我们在纽约市外不提供服务",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="上车地点在纽约市外",
                source_node_index="2",
                target_node_index="4",
            )
        ],
        outgoing_edges=[],
        customer_dependent_action=False,
        kind=JourneyNodeKind.CHAT,
    ),
    "5": _JourneyNode(
        id="5",
        action="询问客户希望的上车时间",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition=None,
                source_node_index="3",
                target_node_index="5",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户已提供上车时间",
                source_node_index="5",
                target_node_index="6",
            )
        ],
        customer_dependent_action=True,
        customer_action_description="客户已提供上车时间",
        kind=JourneyNodeKind.CHAT,
    ),
    "6": _JourneyNode(
        id="6",
        action="按客户要求预订出租车",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户已提供上车时间",
                source_node_index="5",
                target_node_index="6",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="出租车已成功预订",
                source_node_index="6",
                target_node_index="7",
            )
        ],
        customer_dependent_action=False,
        kind=JourneyNodeKind.TOOL,
    ),
    "7": _JourneyNode(
        id="7",
        action="询问客户现金还是刷卡支付",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="出租车已成功预订",
                source_node_index="6",
                target_node_index="7",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择刷卡",
                source_node_index="7",
                target_node_index="8",
            ),
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择现金",
                source_node_index="7",
                target_node_index="9",
            ),
        ],
        customer_dependent_action=True,
        customer_action_description="客户已选择支付方式",
        kind=JourneyNodeKind.CHAT,
    ),
    "8": _JourneyNode(
        id="8",
        action="向客户发送信用卡支付链接",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择刷卡",
                source_node_index="7",
                target_node_index="8",
            )
        ],
        outgoing_edges=[],
        customer_dependent_action=False,
        kind=JourneyNodeKind.CHAT,
    ),
    "9": _JourneyNode(
        id="9",
        action=None,
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择现金",
                source_node_index="7",
                target_node_index="9",
            )
        ],
        outgoing_edges=[],
        customer_dependent_action=False,
        kind=JourneyNodeKind.CHAT,
    ),
}

random_actions_journey_nodes = {
    "1": _JourneyNode(
        id="1",
        action="说一个随机首都城市名，不要再说其他内容。",
        incoming_edges=[],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="上一步已完成",
                source_node_index="1",
                target_node_index="2",
            )
        ],
        customer_dependent_action=False,
        kind=JourneyNodeKind.CHAT,
    ),
    "2": _JourneyNode(
        id="2",
        action="向客户要钱。",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="上一步已完成",
                source_node_index="1",
                target_node_index="2",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="此步骤已完成",
                source_node_index="2",
                target_node_index="3",
            )
        ],
        customer_dependent_action=True,
        kind=JourneyNodeKind.CHAT,
        customer_action_description="客户已直接回应客服的要钱请求",
    ),
    "3": _JourneyNode(
        id="3",
        action="与客户道别并结束对话",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="此步骤已完成",
                source_node_index="2",
                target_node_index="3",
            )
        ],
        outgoing_edges=[],
        customer_dependent_action=False,
        kind=JourneyNodeKind.CHAT,
    ),
}

example_2_expected = JourneyBacktrackNodeSelectionSchema(
    journey_applies=True,
    rationale="客户已提供纽约市内上车地点、目的地和上车时间，可快进经过步骤 2、3、5。下一步必须在步骤 6 停止，因为该步骤需调用工具。",
    requires_backtracking=False,
    step_advancement=[
        JourneyNodeAdvancement(
            id="2", completed=StepCompletionStatus.COMPLETED, follow_ups=["3", "4"]
        ),
        JourneyNodeAdvancement(id="3", completed=StepCompletionStatus.COMPLETED, follow_ups=["5"]),
        JourneyNodeAdvancement(id="5", completed=StepCompletionStatus.COMPLETED, follow_ups=["6"]),
        JourneyNodeAdvancement(id="6", completed=StepCompletionStatus.NEEDS_TOOL_CALL),
    ],
    next_step="6",
)

example_3_events = [
    _make_event(
        "11",
        EventSource.AI_AGENT,
        "欢迎使用我们的出租车服务！请问今天需要什么帮助？",
    ),
    _make_event(
        "23",
        EventSource.CUSTOMER,
        "我想从纽约 34 街西 20 号到肯尼迪机场，现金支付。",
    ),
]

example_3_expected = JourneyBacktrackNodeSelectionSchema(
    journey_applies=True,
    rationale="客户已提供纽约市内上车地点和目的地，可快进经过步骤 1、2、3。步骤 5 需询问上车时间，客户尚未提供。因此应激活步骤 5。",
    requires_backtracking=False,
    step_advancement=[
        JourneyNodeAdvancement(id="1", completed=StepCompletionStatus.COMPLETED, follow_ups=["3"]),
        JourneyNodeAdvancement(
            id="2", completed=StepCompletionStatus.COMPLETED, follow_ups=["3", "4"]
        ),
        JourneyNodeAdvancement(id="3", completed=StepCompletionStatus.COMPLETED, follow_ups=["5"]),
        JourneyNodeAdvancement(id="5", completed=StepCompletionStatus.NEEDS_CUSTOMER_INPUT),
    ],
    next_step="5",
)

example_4_events = [
    _make_event(
        "11",
        EventSource.AI_AGENT,
        "欢迎使用我们的出租车服务！请问今天需要什么帮助？",
    ),
    _make_event(
        "12",
        EventSource.CUSTOMER,
        "我想从纽瓦克机场预约到曼哈顿的出租车。",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "抱歉，我们在纽约市外不提供服务。",
    ),
    _make_event(
        "34",
        EventSource.CUSTOMER,
        "那从肯尼迪机场到时代广场可以吗？",
    ),
    _make_event(
        "45",
        EventSource.AI_AGENT,
        "好的！您要去哪里？",
    ),
    _make_event(
        "56",
        EventSource.CUSTOMER,
        "时代广场。",
    ),
    _make_event(
        "67",
        EventSource.AI_AGENT,
        "请问您希望几点上车？",
    ),
    _make_event(
        "78",
        EventSource.CUSTOMER,
        "我改主意了，能改成从拉瓜迪亚机场上车吗？",
    ),
]

example_4_events = [
    _make_event(
        "11",
        EventSource.AI_AGENT,
        "需要帮您预约出租车吗？",
    ),
    _make_event(
        "12",
        EventSource.CUSTOMER,
        "我想从纽瓦克机场预约到曼哈顿的出租车。",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "抱歉，我们在纽约市外不提供服务。",
    ),
    _make_event(
        "34",
        EventSource.CUSTOMER,
        "那从肯尼迪机场到时代广场可以吗？",
    ),
    _make_event(
        "67",
        EventSource.AI_AGENT,
        "好的！请问您希望几点上车？",
    ),
    _make_event(
        "78",
        EventSource.CUSTOMER,
        "早上 8 点。另外我改主意了，能改成从拉瓜迪亚机场上车吗？",
    ),
]

example_4_expected = JourneyBacktrackNodeSelectionSchema(
    journey_applies=True,
    requires_backtracking=True,
    rationale="客户正在改变在步骤 2 做出的上车地点决定。新的上车地点在纽约市内，因此相关后续为步骤 3。",
    backtracking_target_step="2",
    step_advancement=[
        JourneyNodeAdvancement(
            id="2", completed=StepCompletionStatus.COMPLETED, follow_ups=["3", "4"]
        ),
        JourneyNodeAdvancement(
            id="3",
            completed=StepCompletionStatus.COMPLETED,
            follow_ups=["5"],
        ),
        JourneyNodeAdvancement(
            id="5",
            completed=StepCompletionStatus.COMPLETED,
            follow_ups=["6"],
        ),
        JourneyNodeAdvancement(id="6", completed=StepCompletionStatus.NEEDS_TOOL_CALL),
    ],
    next_step="6",
)

example_5_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "你好，我想预约出租车。",
    ),
    _make_event(
        "12",
        EventSource.AI_AGENT,
        "澳大利亚的首都是堪培拉。",
    ),
    _make_event(
        "23",
        EventSource.CUSTOMER,
        "是吗？我一直以为是悉尼。",
    ),
]

example_5_expected = JourneyBacktrackNodeSelectionSchema(
    journey_applies=True,
    rationale="已向客户介绍了首都。现在需推进到下一步并向客户要钱。",
    requires_backtracking=False,
    step_advancement=[
        JourneyNodeAdvancement(id="1", completed=StepCompletionStatus.COMPLETED, follow_ups=["2"]),
        JourneyNodeAdvancement(id="2", completed=StepCompletionStatus.NEEDS_CUSTOMER_INPUT),
    ],
    next_step="2",
)


# Example 6: Loan Application Journey with branching, backtracking, and completion

example_6_events = [
    _make_event("1", EventSource.CUSTOMER, "你好，我想申请贷款。"),
    _make_event("2", EventSource.AI_AGENT, "好的！请问您的全名是？"),
    _make_event("3", EventSource.CUSTOMER, "简·多伊"),
    _make_event(
        "4", EventSource.AI_AGENT, "您想申请哪种贷款？个人贷款还是企业贷款？"
    ),
    _make_event("5", EventSource.CUSTOMER, "个人贷款"),
    _make_event("6", EventSource.AI_AGENT, "您想贷多少？"),
    _make_event("7", EventSource.CUSTOMER, "50000"),
    _make_event("8", EventSource.AI_AGENT, "您目前的就业状况是？"),
    _make_event(
        "9",
        EventSource.CUSTOMER,
        "我在 Very Important Business Deals 公司担任财务经理。",
    ),
    _make_event(
        "10",
        EventSource.AI_AGENT,
        "请确认您的申请：姓名：简·多伊，类型：个人，金额：50000，职业：Very Important Business Deals 公司财务经理。确认提交？",
    ),
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "我改主意了，想改成企业贷款，是给我工作的公司办的，用他们的车队做抵押，其他贷款信息不变。",
    ),
]

loan_journey_nodes = {
    "1": _JourneyNode(
        id="1",
        action="询问客户全名。",
        incoming_edges=[],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户已提供姓名",
                source_node_index="1",
                target_node_index="2",
            )
        ],
        customer_dependent_action=True,
        kind=JourneyNodeKind.CHAT,
        customer_action_description="客户已提供全名",
    ),
    "2": _JourneyNode(
        id="2",
        action="询问贷款类型：个人或企业。",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户已提供姓名",
                source_node_index="1",
                target_node_index="2",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择个人贷款",
                source_node_index="2",
                target_node_index="3",
            ),
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择企业贷款",
                source_node_index="2",
                target_node_index="4",
            ),
        ],
        customer_dependent_action=True,
        kind=JourneyNodeKind.CHAT,
        customer_action_description="客户已选择贷款类型",
    ),
    "3": _JourneyNode(
        id="3",
        action="询问期望贷款金额。",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择个人贷款",
                source_node_index="2",
                target_node_index="3",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="已提供个人贷款金额",
                source_node_index="3",
                target_node_index="5",
            )
        ],
        customer_dependent_action=True,
        kind=JourneyNodeKind.CHAT,
        customer_action_description="客户已提供期望贷款金额",
    ),
    "4": _JourneyNode(
        id="4",
        action="询问期望贷款金额。",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择企业贷款",
                source_node_index="2",
                target_node_index="4",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="已提供企业贷款金额",
                source_node_index="4",
                target_node_index="6",
            )
        ],
        customer_dependent_action=True,
        customer_action_description="客户已提供期望贷款金额",
        kind=JourneyNodeKind.CHAT,
    ),
    "5": _JourneyNode(
        id="5",
        action="询问就业状况。",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="已提供个人贷款金额",
                source_node_index="3",
                target_node_index="5",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="已提供就业状况",
                source_node_index="5",
                target_node_index="7",
            )
        ],
        customer_dependent_action=True,
        customer_action_description="客户已说明就业状况",
        kind=JourneyNodeKind.CHAT,
    ),
    "6": _JourneyNode(
        id="6",
        action="询问抵押物。",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="已提供企业贷款金额",
                source_node_index="4",
                target_node_index="6",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择数字资产作为抵押",
                source_node_index="6",
                target_node_index="8",
            ),
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择实物资产作为抵押",
                source_node_index="6",
                target_node_index="9",
            ),
        ],
        customer_dependent_action=True,
        customer_action_description="客户已提供抵押物",
        kind=JourneyNodeKind.CHAT,
    ),
    "7": _JourneyNode(
        id="7",
        action="复核并确认申请。",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="已提供就业状况",
                source_node_index="5",
                target_node_index="7",
            )
        ],
        outgoing_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="此步骤已完成",
                source_node_index="7",
                target_node_index="9",
            )
        ],
        customer_dependent_action=True,
        customer_action_description="客户已确认申请及详情",
        kind=JourneyNodeKind.CHAT,
    ),
    "8": _JourneyNode(
        id="8",
        action="复核并确认申请。",
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择数字资产作为抵押",
                source_node_index="6",
                target_node_index="8",
            )
        ],
        outgoing_edges=[],
        customer_dependent_action=True,
        customer_action_description="客户已确认申请及详情",
        kind=JourneyNodeKind.CHAT,
    ),
    "9": _JourneyNode(
        id="9",
        action=None,
        incoming_edges=[
            _JourneyEdge(
                target_guideline=None,
                condition="客户选择实物资产作为抵押",
                source_node_index="6",
                target_node_index="9",
            ),
            _JourneyEdge(
                target_guideline=None,
                condition="此步骤已完成",
                source_node_index="7",
                target_node_index="9",
            ),
        ],
        outgoing_edges=[],
        customer_dependent_action=False,
        kind=JourneyNodeKind.CHAT,
    ),
}

example_6_expected = JourneyBacktrackNodeSelectionSchema(
    journey_applies=True,
    requires_backtracking=True,
    rationale="客户在提供全部信息后改变了贷款类型选择。旅程回退到贷款类型步骤（2），然后根据已提供信息沿企业贷款路径快进，最终退出旅程。",
    backtracking_target_step="2",
    step_advancement=[
        JourneyNodeAdvancement(
            id="2", completed=StepCompletionStatus.COMPLETED, follow_ups=["3", "4"]
        ),
        JourneyNodeAdvancement(
            id="4",
            completed=StepCompletionStatus.COMPLETED,
            follow_ups=["6"],
        ),
        JourneyNodeAdvancement(
            id="6",
            completed=StepCompletionStatus.COMPLETED,
            follow_ups=["8", "None"],
        ),
    ],
    next_step="None",
)

# Example 7: Loan Application Journey where relevant answers were provided earlier in the conversation

example_7_events = [
    _make_event(
        "11",
        EventSource.CUSTOMER,
        "你好，我想贷 1 万美元，用股票做抵押，可以吗？",
    ),
    _make_event(
        "23",
        EventSource.AI_AGENT,
        "可以，请问您想申请哪种类型的贷款？",
    ),
    _make_event(
        "34",
        EventSource.CUSTOMER,
        "什么意思？",
    ),
    _make_event(
        "45",
        EventSource.AI_AGENT,
        "您是想办企业贷款还是个人贷款？",
    ),
    _make_event(
        "56",
        EventSource.CUSTOMER,
        "有区别吗？",
    ),
    _make_event(
        "67",
        EventSource.AI_AGENT,
        "需要先确定类型才能继续申请，两种贷款的要求不同。",
    ),
    _make_event(
        "78",
        EventSource.CUSTOMER,
        "好的，我查一下。",
    ),
    _make_event(
        "89",
        EventSource.AI_AGENT,
        "好的，您慢慢看。",
    ),
    _make_event(
        "78",
        EventSource.CUSTOMER,
        "是给我餐厅办的贷款。",
    ),
]

example_7_expected = JourneyBacktrackNodeSelectionSchema(
    journey_applies=True,
    requires_backtracking=False,
    rationale="客户是为餐厅贷款，属于企业贷款。客户已提供贷款金额和抵押物，可快进经过步骤 4、6，到达步骤 8，该步骤尚未完成。",
    step_advancement=[
        JourneyNodeAdvancement(
            id="2", completed=StepCompletionStatus.COMPLETED, follow_ups=["3", "4"]
        ),
        JourneyNodeAdvancement(id="4", completed=StepCompletionStatus.COMPLETED, follow_ups=["6"]),
        JourneyNodeAdvancement(
            id="6", completed=StepCompletionStatus.COMPLETED, follow_ups=["8", "None"]
        ),
        JourneyNodeAdvancement(
            id="8",
            completed=StepCompletionStatus.NEEDS_CUSTOMER_INPUT,
        ),
    ],
    next_step="8",
)

_baseline_shots: Sequence[JourneyNodeSelectionShot] = [
    JourneyNodeSelectionShot(
        description="示例 1 - 简单单步推进",
        journey_title="度假推荐旅程",
        interaction_events=example_1_events,
        journey_nodes=example_1_journey_nodes,
        expected_result=example_1_expected,
        previous_path=["1"],
        conditions=["客户对度假感兴趣"],
    ),
    JourneyNodeSelectionShot(
        description="示例 2 - 多步推进在需调用工具的步骤处停止",
        journey_title="预约出租车旅程",
        interaction_events=example_2_events,
        journey_nodes=book_taxi_shot_journey_nodes,
        expected_result=example_2_expected,
        previous_path=["1", "2"],
        conditions=[],
    ),
    JourneyNodeSelectionShot(
        description="示例 3 - 多步推进因缺少信息而停止",
        journey_title="预约出租车旅程 - 与示例 2 相同",
        interaction_events=example_3_events,
        journey_nodes=None,
        expected_result=example_3_expected,
        previous_path=["1"],
        conditions=[],
    ),
    JourneyNodeSelectionShot(
        description="示例 4 - 因客户改变决定而回退",
        journey_title="预约出租车旅程 - 与示例 2 相同",
        interaction_events=example_4_events,
        journey_nodes=None,
        expected_result=example_4_expected,
        previous_path=["1", "2", "4", "2", "3", "5"],
        conditions=[],
    ),
    JourneyNodeSelectionShot(
        description="示例 5 - 除非客户明确表示否则保持在旅程中",
        journey_title="预约出租车 II 旅程",
        interaction_events=example_5_events,
        journey_nodes=random_actions_journey_nodes,
        expected_result=example_5_expected,
        previous_path=["1"],
        conditions=["客户想预约出租车"],
    ),
    JourneyNodeSelectionShot(
        description="示例 6 - 回退并快进至完成",
        journey_title="贷款申请旅程",
        interaction_events=example_6_events,
        journey_nodes=loan_journey_nodes,
        expected_result=example_6_expected,
        previous_path=["1", "2", "3", "5", "7"],
        conditions=["客户想申请贷款"],
    ),
    JourneyNodeSelectionShot(
        description="示例 7 - 因对话中提前提供的信息而快进",
        journey_title="贷款申请旅程 - 与示例 6 相同",
        interaction_events=example_7_events,
        journey_nodes=loan_journey_nodes,
        expected_result=example_7_expected,
        previous_path=["1", "2"],
        conditions=["客户想申请贷款"],
    ),
]


shot_collection = ShotCollection[JourneyNodeSelectionShot](_baseline_shots)
