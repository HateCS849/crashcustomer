"""Executable resource-contention model: real SQLite leases and HTTP dependency calls.

This models the failure mechanism; it is not Hangari's Java/Hikari implementation.
"""
import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx


class PoolTimeout(Exception):
    pass


class ConnectionPool:
    def __init__(self, path: Path, size: int, timeout_ms: int):
        self.size, self.timeout_ms = size, timeout_ms
        self.available = asyncio.Queue()
        self.connections = []
        self.owners = {}
        self.waiting = self.peak_waiting = self.peak_active = self.timeouts = 0
        self.hold_times = {"login": [], "read": [], "log": []}
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as init:
            init.executescript("""PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS foods(id INTEGER PRIMARY KEY, name TEXT);
                INSERT OR IGNORE INTO foods VALUES(1,'김치'),(2,'비빔밥'),(3,'떡볶이');
                CREATE TABLE IF NOT EXISTS error_log(id INTEGER PRIMARY KEY, payload TEXT);
            """)
        for _ in range(size):
            connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=.2)
            self.connections.append(connection)
            self.available.put_nowait(connection)

    @asynccontextmanager
    async def lease(self, owner: str):
        self.waiting += 1
        self.peak_waiting = max(self.peak_waiting, self.waiting)
        try:
            connection = await asyncio.wait_for(self.available.get(), self.timeout_ms / 1000)
        except TimeoutError as error:
            self.timeouts += 1
            raise PoolTimeout("DB 커넥션 획득 대기 시간 초과") from error
        finally:
            self.waiting -= 1
        started = time.perf_counter()
        self.owners[id(connection)] = owner
        self.peak_active = max(self.peak_active, len(self.owners))
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.rollback()
            self.hold_times[owner.split(":", 1)[0]].append((time.perf_counter() - started) * 1000)
            self.owners.pop(id(connection), None)
            self.available.put_nowait(connection)

    def snapshot(self):
        return {"size": self.size, "active": len(self.owners), "idle": self.available.qsize(),
                "waiting": self.waiting, "peak_active": self.peak_active, "peak_waiting": self.peak_waiting,
                "acquisition_timeouts": self.timeouts,
                "owners": {role: sum(owner.startswith(role + ":") for owner in self.owners.values()) for role in self.hold_times},
                "hold_max_ms": {role: round(max(values, default=0), 2) for role, values in self.hold_times.items()}}

    def close(self):
        for connection in self.connections:
            connection.close()


class ErrorRecorder:
    def __init__(self, runtime, asynchronous: bool):
        self.runtime, self.asynchronous = runtime, asynchronous
        self.queue = asyncio.Queue(maxsize=runtime.config["log_queue_capacity"])
        self.path = runtime.directory / "fallback.jsonl"
        self.file_lock = asyncio.Lock()
        self.received = self.db_saved = self.file_saved = self.failed = self.peak_depth = 0
        self.worker = None

    async def start(self):
        if self.asynchronous:
            self.worker = asyncio.create_task(self._worker())

    async def record(self, record: dict):
        self.received += 1
        if self.asynchronous:
            try:
                self.queue.put_nowait(record)
                self.peak_depth = max(self.peak_depth, self.queue.qsize())
            except asyncio.QueueFull:
                await self._file(record)
        else:
            await self._database(record, fallback=False)

    async def _database(self, record, fallback: bool):
        try:
            async with self.runtime.pool.lease("log:" + record["user"]) as connection:
                await asyncio.sleep(self.runtime.config["log_write_ms"] / 1000)
                connection.execute("INSERT INTO error_log(payload) VALUES (?)", (json.dumps(record, ensure_ascii=False),))
                connection.commit()
                self.db_saved += 1
        except (PoolTimeout, sqlite3.Error):
            if fallback:
                await self._file(record)
            else:
                self.failed += 1

    async def _file(self, record):
        line = json.dumps(record, ensure_ascii=False) + "\n"
        async with self.file_lock:
            def append():
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(line)
            await asyncio.to_thread(append)
            self.file_saved += 1

    async def _worker(self):
        while True:
            record = await self.queue.get()
            try:
                await self._database(record, fallback=True)
            except asyncio.CancelledError:
                await self._file(record)
                raise
            finally:
                self.queue.task_done()

    async def stop(self):
        if self.worker:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=2)
            except TimeoutError:
                pass
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            while not self.queue.empty():
                record = self.queue.get_nowait()
                await self._file(record)
                self.queue.task_done()

    def snapshot(self):
        return {"mode": "bounded_async" if self.asynchronous else "synchronous",
                "capacity": self.queue.maxsize, "depth": self.queue.qsize(), "peak_depth": self.peak_depth,
                "received": self.received, "db_saved": self.db_saved, "file_saved": self.file_saved, "failed": self.failed}


class LabRuntime:
    def __init__(self, config: dict, variant: str, directory: Path, client: httpx.AsyncClient):
        self.config, self.variant, self.directory, self.client = config, variant, directory, client
        directory.mkdir(parents=True, exist_ok=True)
        self.pool = ConnectionPool(directory / "service.sqlite3", config["pool_size"], config["pool_acquire_ms"])
        self.logs = ErrorRecorder(self, asynchronous=variant == "after")
        self.mock_stop, self.dependency_ready = asyncio.Event(), asyncio.Event()
        self.mock_calls = self.mock_active = self.mock_peak = 0
        self.http_active = self.http_peak = 0
        self.completed = 0

    async def apple(self):
        self.mock_calls += 1
        self.mock_active += 1
        self.mock_peak = max(self.mock_peak, self.mock_active)
        if self.mock_calls >= min(self.config["users"], self.config["pool_size"]):
            self.dependency_ready.set()
        try:
            try:
                await asyncio.wait_for(self.mock_stop.wait(), self.config["external_delay_ms"] / 1000)
                return 503, {"error": "experiment_stopped"}
            except TimeoutError:
                if self.config["external_result"] == "error":
                    return 503, {"error": "dependency_failure"}
                return 200, {"subject": "synthetic-user"}
        finally:
            self.mock_active -= 1

    async def _external(self, token: str):
        async def call():
            response = await self.client.post(f"/lab/service/{token}/apple")
            response.raise_for_status()
        if self.variant == "after":
            async with asyncio.timeout(self.config["external_timeout_ms"] / 1000):
                await call()
        else:
            await call()

    async def handle(self, endpoint: str, user: str, token: str):
        self.http_active += 1
        self.http_peak = max(self.http_peak, self.http_active)
        try:
            if endpoint == "login":
                if self.variant == "before":
                    async with self.pool.lease("login:" + user) as connection:
                        connection.execute("SELECT name FROM foods LIMIT 1").fetchone()
                        try:
                            await self._external(token)
                        except (httpx.HTTPError, TimeoutError) as error:
                            # Deliberately reproduce a second DB acquisition for failure logging
                            # while the outer login transaction still owns its connection.
                            return await self._failure(user, error)
                else:
                    await self._external(token)
                    async with self.pool.lease("login:" + user) as connection:
                        connection.execute("SELECT name FROM foods LIMIT 1").fetchone()
                return 200, {"ok": True, "endpoint": endpoint}
            async with self.pool.lease("read:" + user) as connection:
                rows = connection.execute("SELECT name FROM foods WHERE name LIKE ?", ("%김%" if endpoint == "search" else "%",)).fetchall()
            return 200, {"ok": True, "items": [row[0] for row in rows]}
        except (PoolTimeout, httpx.HTTPError, TimeoutError) as error:
            return await self._failure(user, error)
        finally:
            self.http_active -= 1
            self.completed += 1

    async def _failure(self, user: str, error: Exception):
        reason = "db_pool_timeout" if isinstance(error, PoolTimeout) else "external_timeout" if isinstance(error, TimeoutError) else "external_error"
        await self.logs.record({"user": user, "reason": reason, "time": time.time()})
        return (503 if reason == "db_pool_timeout" else 504 if reason == "external_timeout" else 502), {"ok": False, "reason": reason}

    def snapshot(self):
        return {"pool": self.pool.snapshot(), "logs": self.logs.snapshot(),
                "http_active": self.http_active, "http_peak": self.http_peak, "completed": self.completed,
                "mock_calls": self.mock_calls, "mock_active": self.mock_active, "mock_peak": self.mock_peak}

    async def close(self):
        self.mock_stop.set()
        await self.logs.stop()
        self.pool.close()
