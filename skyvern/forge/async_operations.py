import asyncio
from enum import StrEnum

import structlog
from playwright.async_api import Page

LOG = structlog.get_logger()


class AgentPhase(StrEnum):
    """
    Phase of agent when async execution events are happening
    """

    action = "action"
    scrape = "scrape"
    llm = "llm"


VALID_AGENT_PHASES = [phase.value for phase in AgentPhase]


class AsyncOperation:
    """
    AsyncOperation can take async actions on the page while agent is performing the task.

    Examples:
        - collect info based on the html/DOM and send data to your server
    """

    def __init__(self, task_id: str, operation_type: str, agent_phase: AgentPhase, page: Page) -> None:
        """
        :param task_id: task_id of the task
        :param operation_type: it's the custom type of the operation.
            there will only be up to one aio task running per operation_type
        :param agent_phase: AgentPhase type. phase of the agent when the operation is running
        :param page: playwright page for the task
        """
        self.task_id = task_id
        self.type = operation_type
        self.agent_phase = agent_phase
        self.aio_task: asyncio.Task | None = None

        # playwright page could be used by the operation to take actions
        self.page = page

    async def execute(self) -> None:
        return

    def run(self) -> asyncio.Task | None:
        if self.aio_task is not None and not self.aio_task.done():
            LOG.warning(
                f"Task already running",
                task_id=self.task_id,
                operation_type=self.type,
                agent_phase=self.agent_phase,
            )
            return None
        self.aio_task = asyncio.create_task(self.execute())
        return self.aio_task


class AsyncOperationPool:
    _operations: dict[str, dict[AgentPhase, AsyncOperation]] = {}  # task_id: {agent_phase: operation}

    # use _aio_tasks to ensure we're only execution one aio task for the same operation_type
    _aio_tasks: dict[str, dict[str, asyncio.Task]] = {}  # task_id: {operation_type: aio_task}

    def _add_operation(self, task_id: str, operation: AsyncOperation) -> None:
        if operation.agent_phase not in VALID_AGENT_PHASES:
            raise ValueError(f"operation's agent phase {operation.agent_phase} is not valid")
        if task_id not in self._operations:
            self._operations[task_id] = {}
        self._operations[task_id][operation.agent_phase] = operation

    def add_operations(self, task_id: str, operations: list[AsyncOperation]) -> None:
        if task_id in self._operations:
            # already exists
            return
        for operation in operations:
            self._add_operation(task_id, operation)

    def _get_operation(self, task_id: str, operation_type: AgentPhase) -> AsyncOperation | None:
        return self._operations.get(task_id, {}).get(operation_type, None)

    def remove_operations(self, task_id: str) -> None:
        if task_id in self._operations:
            del self._operations[task_id]

    def get_aio_tasks(self, task_id: str) -> list[asyncio.Task]:
        """
        Get all the running/pending aio tasks for the given task_id
        """
        return [aio_task for aio_task in self._aio_tasks.get(task_id, {}).values() if not aio_task.done()]

    def run_operation(self, task_id: str, agent_phase: AgentPhase) -> None:
        # get the operation from the pool
        operation = self._get_operation(task_id, agent_phase)
        if operation is None:
            return

        # if found, initialize the operation if it's the first time running the aio task
        operation_type = operation.type
        if task_id not in self._aio_tasks:
            self._aio_tasks[task_id] = {}

        # if the aio task is already running, don't run it again
        aio_task: asyncio.Task | None = None
        if operation_type in self._aio_tasks[task_id]:
            aio_task = self._aio_tasks[task_id][operation_type]
            if not aio_task.done():
                LOG.info(
                    f"aio task already running",
                    task_id=task_id,
                    operation_type=operation_type,
                    agent_phase=agent_phase,
                )
                return

        # run the operation if the aio task is not running
        aio_task = operation.run()
        if aio_task:
            self._aio_tasks[task_id][operation_type] = aio_task

    async def remove_task(self, task_id: str) -> None:
        try:
            async with asyncio.timeout(30):
                await asyncio.gather(*[aio_task for aio_task in self.get_aio_tasks(task_id) if not aio_task.done()])
        except asyncio.TimeoutError:
            LOG.error(f"Timeout (30s) while waiting for pending async tasks for task_id={task_id}", task_id=task_id)

        self.remove_operations(task_id)
