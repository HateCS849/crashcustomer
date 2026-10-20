"""SQLite persistence. Demo fault configuration is never passed to the actor."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.artifacts = directory / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.uploads = directory / "uploads"
        self.uploads.mkdir(exist_ok=True)
        self.db = directory / "crashcustomer.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, fault TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    submission TEXT
                );
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attachments_project ON attachments(project_id);
                CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_project(self, values: dict) -> dict:
        project = {"id": uuid.uuid4().hex, **values, "created_at": now(), "updated_at": now()}
        with self.connect() as conn:
            conn.execute("INSERT INTO projects VALUES (?,?)", (project["id"], json.dumps(project, ensure_ascii=False)))
        return project

    def create_experiment(self, config: dict) -> dict:
        experiment = {"id": uuid.uuid4().hex, "config": config, "status": "queued", "stage": "queued",
                      "created_at": now(), "finished_at": None, "cases": [], "events": [], "findings": [],
                      "ai_analysis": None, "ai_error": None, "error": None}
        self.save_experiment(experiment)
        return experiment

    def save_experiment(self, experiment: dict):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO experiments VALUES (?,?)", (experiment["id"], json.dumps(experiment, ensure_ascii=False)))

    def get_experiment(self, experiment_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_experiments(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM experiments ORDER BY rowid DESC LIMIT 30").fetchall()
        return [json.loads(row[0]) for row in rows]

    def recover_experiments(self):
        for run in self.list_experiments():
            if run["status"] in {"queued", "running"}:
                run.update(status="interrupted", finished_at=now(), error="서버 종료로 실험이 중단되었습니다.")
                self.save_experiment(run)

    def create_campaign(self, project: dict, config: dict) -> dict:
        campaign = {"id": uuid.uuid4().hex, "project_id": project["id"], "project_name": project["name"],
                    "target_url": project["browser_url"], "config": config, "status": "queued", "stage": "queued",
                    "created_at": now(), "finished_at": None, "events": [], "run_ids": [], "observed_requests": [],
                    "observed_assets": [], "references": [], "candidates": [], "scenarios": [], "probes": None, "summary": None, "error": None,
                    "usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0}}
        self.save_campaign(campaign)
        return campaign

    def save_campaign(self, campaign: dict):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO campaigns VALUES (?,?,?)",
                         (campaign["id"], campaign["project_id"], json.dumps(campaign, ensure_ascii=False)))

    def get_campaign(self, campaign_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_campaigns(self, project_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT payload FROM campaigns WHERE project_id=? ORDER BY rowid DESC LIMIT 20", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT payload FROM campaigns ORDER BY rowid DESC LIMIT 20").fetchall()
        return [json.loads(row[0]) for row in rows]

    def recover_campaigns(self):
        for campaign in self.list_campaigns():
            if campaign["status"] in {"queued", "running"}:
                campaign.update(status="interrupted", finished_at=now(), error="서버 종료로 사전 점검이 중단되었습니다.")
                self.save_campaign(campaign)

    def get_project(self, project_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM projects WHERE id=?", (project_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def update_project(self, project_id: str, values: dict) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise KeyError(project_id)
            project = {**json.loads(row[0]), **values, "updated_at": now()}
            conn.execute("UPDATE projects SET payload=? WHERE id=?", (json.dumps(project, ensure_ascii=False), project_id))
        return project

    def list_projects(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("""SELECT p.payload, COUNT(a.id) FROM projects p
                                   LEFT JOIN attachments a ON a.project_id=p.id
                                   GROUP BY p.id ORDER BY p.rowid DESC""").fetchall()
        return [{**json.loads(payload), "attachment_count": count} for payload, count in rows]

    def list_attachments(self, project_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM attachments WHERE project_id=? ORDER BY rowid DESC", (project_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_attachment(self, project_id: str, attachment_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM attachments WHERE id=? AND project_id=?", (attachment_id, project_id)).fetchone()
        return json.loads(row[0]) if row else None

    def add_attachment(self, project_id: str, filename: str, content: bytes, analysis: dict) -> dict:
        import hashlib

        attachment_id = uuid.uuid4().hex
        attachment = {"id": attachment_id, "project_id": project_id, "filename": filename,
                      "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                      "created_at": now(), **analysis}
        # Storage names are generated here; uploaded paths never become filesystem paths.
        destination = self.uploads / attachment_id
        destination.write_bytes(content)
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                    raise KeyError(project_id)
                if conn.execute("SELECT COUNT(*) FROM attachments WHERE project_id=?", (project_id,)).fetchone()[0] >= 30:
                    raise ValueError("프로젝트당 최대 30개 파일을 보관할 수 있습니다.")
                conn.execute("INSERT INTO attachments VALUES (?,?,?)", (attachment_id, project_id, json.dumps(attachment, ensure_ascii=False)))
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return attachment

    def remove_attachment(self, project_id: str, attachment_id: str) -> bool:
        with self.connect() as conn:
            result = conn.execute("DELETE FROM attachments WHERE id=? AND project_id=?", (attachment_id, project_id))
        if result.rowcount:
            (self.uploads / attachment_id).unlink(missing_ok=True)
        return bool(result.rowcount)

    def create_run(self, request: dict) -> dict:
        run_id = uuid.uuid4().hex
        run = {"id": run_id, **request, "status": "queued", "created_at": now(),
               "finished_at": None, "events": [], "actions": [], "observations": [],
               "findings": [], "replays": [], "error": None, "result": None,
               "usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0}}
        self.save_run(run)
        return run

    def save_run(self, run: dict):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?)",
                         (run["id"], json.dumps(run, ensure_ascii=False)))

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_runs(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM runs ORDER BY rowid DESC LIMIT 30").fetchall()
        return [json.loads(row[0]) for row in rows]

    def recover_interrupted(self):
        for run in self.list_runs():
            if run["status"] in {"queued", "running", "replaying"}:
                run.update(status="interrupted", error="서버가 종료되어 실행이 중단되었습니다. 새 실행을 시작하세요.", finished_at=now())
                self.save_run(run)

    def create_session(self, fault: str) -> str:
        token = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("INSERT INTO sessions (id,fault) VALUES (?,?)", (token, fault))
        return token

    def get_session(self, token: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT fault,attempts,submission FROM sessions WHERE id=?", (token,)).fetchone()
        return dict(zip(("fault", "attempts", "submission"), row)) if row else None

    def submit(self, token: str, applicant: dict) -> tuple[int, dict]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT attempts,submission FROM sessions WHERE id=?", (token,)).fetchone()
            if row is None:
                return 404, {"message": "신청 페이지를 찾을 수 없습니다."}
            if not applicant["consent"] or "@" not in applicant["email"] or not applicant["name"].strip():
                return 422, {"message": "입력 내용을 확인해주세요."}
            conn.execute("UPDATE sessions SET attempts=attempts+1 WHERE id=?", (token,))
            # Every variant has the SAME deterministic transient failure on its first valid submission.
            if row[0] == 0:
                return 503, {"message": "일시적으로 신청을 처리할 수 없습니다. 다시 시도해주세요."}
            if row[1]:
                return 200, {"message": "신청이 완료되었습니다.", "registration_id": token[:8]}
            conn.execute("UPDATE sessions SET submission=? WHERE id=?", (json.dumps(applicant), token))
            return 201, {"message": "신청이 완료되었습니다.", "registration_id": token[:8]}
