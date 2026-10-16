"""Run inside the api container: a checkpointer turn (sets up `langgraph`) and both workers'
tasks (for ids that don't exist, so each just queries and returns)."""

import asyncio

import psycopg

from aios import checkpoints
from aios.worker import celery_app


async def turn() -> None:
    async with checkpoints.turn(run_id=0):
        pass
    conn = await psycopg.AsyncConnection.connect(checkpoints.conninfo())
    async with conn:
        user = (await (await conn.execute("SELECT current_user")).fetchone())[0]
    print("checkpointer turn ok as", user)


asyncio.run(turn())
celery_app.send_task("aios.ingest_document", args=[999999])
celery_app.send_task("aios.run_workflow", args=[999999], queue="workflows")
print("enqueued aios.ingest_document and aios.run_workflow")
