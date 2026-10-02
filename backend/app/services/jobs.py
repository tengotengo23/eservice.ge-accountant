"""API -> worker დავალებების რიგი. job_id დედუპლიკაციისთვის: იგივე id-ით მეორე დავალება არ იქმნება."""

from __future__ import annotations

from typing import Protocol

# worker-ის ფუნქციების სახელები (app/worker.py) - API და worker ერთმანეთის მოდულებს არ აიმპორტებენ
PREPARE = "prepare_declaration"
SUBMIT = "submit_declaration"


class JobQueue(Protocol):
    async def enqueue(self, function: str, *args: str, job_id: str) -> bool:
        """True - ჩადგა რიგში; False - იგივე job_id უკვე არსებობს (დუბლიკატი)."""
        ...


class ArqJobQueue:
    def __init__(self, pool):
        self._pool = pool  # arq.connections.ArqRedis

    async def enqueue(self, function: str, *args: str, job_id: str) -> bool:
        return await self._pool.enqueue_job(function, *args, _job_id=job_id) is not None
