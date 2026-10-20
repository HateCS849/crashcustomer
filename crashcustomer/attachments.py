"""Inspect local project attachments without executing code, extracting ZIPs, or resolving URLs."""
import io
import json
import re
import stat
import zipfile
from collections import Counter
from pathlib import PurePosixPath

import yaml

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
TEXT_EXTENSIONS = {".json", ".yaml", ".yml", ".md", ".txt", ".java", ".kt", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".gradle", ".xml", ".sql", ".csv", ".properties"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | {".zip", ".pdf", ".xlsx"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
SECRET_MARKERS = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
IGNORED_PARTS = {".git", ".venv", "node_modules", ".gradle", "__pycache__", ".idea"}


class AttachmentError(ValueError):
    pass


class DocumentLoader(yaml.SafeLoader):
    """Bound YAML nesting and reject aliases to avoid recursive/exponential documents."""
    depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise AttachmentError("YAML 별칭은 지원하지 않습니다. 값을 펼친 JSON/YAML로 첨부해주세요.")
        self.depth += 1
        if self.depth > 80:
            raise AttachmentError("문서의 중첩 단계가 너무 깊습니다.")
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


def validate_name(filename: str):
    if not filename or len(filename) > 180 or "/" in filename or "\\" in filename or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise AttachmentError("파일 이름은 경로 없이 180자 이내로 지정해주세요.")
    check_sensitive_path(filename)


def check_sensitive_path(name: str):
    path = PurePosixPath(name)
    lower = path.name.lower()
    if any(part.lower() in IGNORED_PARTS for part in path.parts):
        raise AttachmentError(".git, .venv, node_modules 등 실행·저장소 폴더는 ZIP에서 제외해주세요.")
    if ((lower == ".env" or lower.startswith(".env.")) and lower not in {".env.example", ".env.sample"}) or lower.endswith((".p8", ".pem", ".key", ".p12", ".pfx", ".jks")) or lower in {"id_rsa", "id_ed25519", "credentials"} or "accesskeys" in lower:
        raise AttachmentError("개인키·접근키·실제 환경변수 파일을 제외한 뒤 첨부해주세요: " + path.name)


def load_document(content: bytes, suffix: str):
    if len(content) > MAX_TEXT_BYTES:
        raise AttachmentError("텍스트·명세 파일은 2MB까지 지원합니다.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise AttachmentError("텍스트 파일은 UTF-8 인코딩으로 첨부해주세요.") from error
    if suffix in {".json", ".yaml", ".yml"}:
        try:
            data = json.loads(text) if suffix == ".json" else yaml.load(text, Loader=DocumentLoader)
        except AttachmentError:
            raise
        except (ValueError, yaml.YAMLError, RecursionError) as error:
            raise AttachmentError("JSON/YAML 문법을 확인해주세요. 실행 가능한 YAML 태그는 지원하지 않습니다.") from error
        return text, data
    return text, None


def resolve_local(document: dict, value):
    if not isinstance(value, dict):
        return {}
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        target = document
        for part in reference[2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(target, dict) else None
        return target if isinstance(target, dict) else {}
    return value


def openapi_summary(document: dict) -> dict:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise AttachmentError("OpenAPI 문서에 paths 객체가 필요합니다.")
    if len(paths) > 5000:
        raise AttachmentError("OpenAPI 경로는 최대 5,000개까지 지원합니다.")
    operations = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters = []
            shared = path_item.get("parameters", [])
            own = operation.get("parameters", [])
            for parameter in (shared if isinstance(shared, list) else []) + (own if isinstance(own, list) else []):
                parameter = resolve_local(document, parameter)
                parameters.append({"name": str(parameter.get("name", ""))[:120], "in": str(parameter.get("in", ""))[:30], "required": parameter.get("required") is True})
            operations.append({"method": method.upper(), "path": path[:500],
                               "summary": str(operation.get("summary", ""))[:300],
                               "authenticated": bool(operation.get("security", document.get("security", []))),
                               "parameters": parameters[:40]})
    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    return {"title": str(info.get("title", "API 명세"))[:200], "version": str(info.get("version", ""))[:80],
            "operation_count": len(operations), "operations": operations[:200],
            "notice": "첨부 문서에서 읽은 명세입니다. 실제 서버 동작·명세 일치 여부는 아직 검증하지 않았습니다."}


def archive_summary(content: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > 1000 or sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES:
                raise AttachmentError("ZIP은 항목 1,000개·압축 해제 크기 50MB까지 지원합니다.")
            files, extensions = [], Counter()
            for member in members:
                name = member.filename
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name or (path.parts and ":" in path.parts[0]) or any(ord(c) < 32 for c in name):
                    raise AttachmentError("ZIP에 허용되지 않는 경로가 있습니다.")
                if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                    raise AttachmentError("심볼릭 링크·암호화 ZIP은 지원하지 않습니다.")
                check_sensitive_path(name)
                if member.is_dir():
                    continue
                suffix = path.suffix.lower()
                if suffix == ".zip":
                    raise AttachmentError("중첩 ZIP을 제외한 뒤 첨부해주세요.")
                if suffix in TEXT_EXTENSIONS or path.name in {".env.example", ".env.sample"}:
                    if member.file_size > MAX_TEXT_BYTES:
                        raise AttachmentError("ZIP 내부 텍스트 파일은 각각 2MB 이하여야 합니다.")
                    if SECRET_MARKERS.search(archive.read(member)):
                        raise AttachmentError("ZIP에 개인키 또는 AWS 접근키 패턴이 포함되어 있습니다.")
                files.append({"path": name[:300], "size": member.file_size})
                extensions[suffix or "확장자 없음"] += 1
            return {"file_count": len(files), "uncompressed_size": sum(m.file_size for m in members),
                    "files": files[:200], "extensions": dict(extensions),
                    "notice": "ZIP 내부 목록만 확인했습니다. 코드를 추출하거나 실행하지 않았습니다."}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as error:
        raise AttachmentError("읽을 수 있는 일반 ZIP 파일을 첨부해주세요.") from error


def inspect_upload(filename: str, content: bytes, category: str = "auto") -> dict:
    validate_name(filename)
    if not content:
        raise AttachmentError("비어 있는 파일은 첨부할 수 없습니다.")
    if len(content) > MAX_FILE_BYTES:
        raise AttachmentError("파일 하나당 최대 20MB까지 첨부할 수 있습니다.")
    if category not in {"auto", "openapi", "test_plan", "reference"}:
        raise AttachmentError("첨부 자료의 종류를 확인해주세요.")
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise AttachmentError("JSON, YAML, ZIP, 문서 또는 지원되는 코드 파일을 첨부해주세요.")
    if SECRET_MARKERS.search(content):
        raise AttachmentError("개인키 또는 AWS 접근키 패턴이 포함된 파일은 첨부할 수 없습니다.")
    if suffix == ".zip":
        if category not in {"auto", "reference"}:
            raise AttachmentError("API 명세·실험 계획은 ZIP 대신 JSON/YAML 파일로 첨부해주세요.")
        return {"category": "reference", "format": "zip", "summary": archive_summary(content)}
    if suffix in {".pdf", ".xlsx"}:
        if category not in {"auto", "reference"}:
            raise AttachmentError("API 명세·실험 계획은 JSON/YAML 형식으로 첨부해주세요.")
        if suffix == ".pdf" and not content.startswith(b"%PDF-"):
            raise AttachmentError("올바른 PDF 파일을 첨부해주세요.")
        if suffix == ".xlsx":
            # Validate container paths and sizes without parsing cell contents or extracting files.
            archive_summary(content)
        return {"category": "reference", "format": suffix[1:], "summary": {"notice": "참고 파일로 보관합니다. 내용 자동 분석은 아직 지원하지 않습니다."}}
    text, document = load_document(content, suffix)
    is_openapi = isinstance(document, dict) and (str(document.get("openapi", "")).startswith("3.") or document.get("swagger") == "2.0")
    if category == "openapi" and not is_openapi:
        raise AttachmentError("OpenAPI 3.x 또는 Swagger 2.0 명세를 첨부해주세요.")
    if is_openapi and category in {"auto", "openapi"}:
        return {"category": "openapi", "format": suffix[1:], "summary": openapi_summary(document)}
    if category == "test_plan":
        if suffix not in {".json", ".yaml", ".yml"} or not isinstance(document, dict):
            raise AttachmentError("실험 계획은 JSON/YAML 객체 형식으로 첨부해주세요.")
        return {"category": "test_plan", "format": suffix[1:], "summary": {"sections": [str(k)[:100] for k in list(document)[:30]], "notice": "실험 계획을 보관했습니다. 실행 가능한 계획인지 검증하거나 실행하지 않았습니다."}}
    return {"category": "reference", "format": suffix[1:], "summary": {"line_count": len(text.splitlines()), "notice": "참고 자료로 보관합니다. 첨부 내용은 지시문으로 실행하지 않습니다."}}


TEST_PLAN_TEMPLATE = """name: external-login-isolation
description: 외부 로그인 지연 중 목록과 검색의 영향 범위 비교
variants:
  before: http://localhost:10000
  after: http://localhost:10010
external_dependency:
  name: apple-mock
  delay_ms: 10000
load:
  concurrent_requests: 70
  note: 로그인·목록·검색의 요청 수와 발사 시점을 추가로 명시해주세요.
assertions:
  login_max_ms: 3000
  contents_success_rate: 1.0
  search_success_rate: 1.0
observations:
  - db_connections_active
  - db_connections_pending
  - error_log_queue_depth
# 이 파일은 등록용 예시입니다. 아직 자동 실행되지 않습니다.
"""
