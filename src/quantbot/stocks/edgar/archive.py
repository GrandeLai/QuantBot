"""有界 SEC 季度索引采集和不可变文件版本；受理时间不冒充公开时间。"""

from contextlib import contextmanager
from datetime import UTC, date, datetime
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from quantbot.ai.journal import runtime_lock
from quantbot.ai.vintages import instant


EASTERN = ZoneInfo("America/New_York")
ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
SEC = "https://www.sec.gov/Archives/"


def cik_id(value):
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,10}", text) or int(text) == 0:
        raise ValueError("CIK 必须为 1..10 位非零数字")
    return text.zfill(10)


def quarters(start, end):
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if start > end or start < date(1994, 7, 1):
        raise ValueError("SEC 日期范围无效")
    year, quarter = start.year, (start.month - 1) // 3 + 1
    last = (end.year, (end.month - 1) // 3 + 1)
    result = []
    while (year, quarter) <= last:
        result.append({"year": year, "quarter": quarter, "url": f"{SEC}edgar/full-index/{year}/QTR{quarter}/master.idx"})
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    if len(result) > 8:
        raise ValueError("一次采集最多跨 8 个季度，请分批执行")
    return result


def parse_master(text, start, end, ciks):
    lines = text.splitlines()
    header = "CIK|Company Name|Form Type|Date Filed|Filename"
    try:
        index = next(i for i, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        raise ValueError("不是有效的 SEC master.idx，不能当作空季度") from None
    result = {}
    for line in lines[index + 1:]:
        if not line.strip() or set(line.strip()) == {"-"}:
            continue
        parts = line.split("|")
        if len(parts) != 5:
            raise ValueError("SEC 索引存在损坏行")
        cik, company, form, filed, path = parts
        cik = cik_id(cik)
        filed = date.fromisoformat(filed)
        if form not in {"8-K", "8-K/A"} or cik not in ciks or not start <= filed <= end:
            continue
        match = re.fullmatch(r"edgar/data/([0-9]+)/([0-9]{10}-[0-9]{2}-[0-9]{6})\.txt", path)
        if match is None or cik_id(match[1]) != cik:
            raise ValueError("SEC 索引文件路径与发行人不一致")
        accession = match[2]
        row = {"cik": cik, "company": company, "form": form, "filing_date": str(filed),
               "accession": accession, "source_url": SEC + path}
        if accession in result and result[accession] != row:
            raise ValueError("同一 accession 的索引元数据冲突")
        result[accession] = row
    return list(result.values())


class FilingText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.ignored = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "ix:hidden"}:
            self.ignored += 1
        if tag in {"p", "div", "br", "tr", "td", "table", "h1", "h2", "h3", "li"} and not self.ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "ix:hidden"}:
            self.ignored = max(0, self.ignored - 1)
        if tag in {"p", "div", "tr", "td", "h1", "h2", "h3", "li"} and not self.ignored:
            self.parts.append("\n")

    def handle_data(self, value):
        if not self.ignored:
            self.parts.append(value)

    def text(self):
        return "\n".join(line for line in (re.sub(r"\s+", " ", s).strip() for s in "".join(self.parts).splitlines()) if line)


def parse_submission(text, index_row, first_seen_at):
    """只解析完整文件的 header 和与 form 相同的主文档，不猜第一个 HTML 链接。"""
    first_seen = instant(first_seen_at)
    header = re.split(r"<DOCUMENT>", text, maxsplit=1, flags=re.I)[0]

    def header_value(pattern, name, optional=False):
        values = re.findall(pattern, header, re.I | re.M)
        if len(values) != 1:
            if optional and not values:
                return None
            raise ValueError(f"SEC 文件缺少或重复 {name}")
        return values[0].strip()

    accession = header_value(r"^\s*ACCESSION NUMBER:\s*([^\r\n]+)", "accession")
    form = header_value(r"^\s*CONFORMED SUBMISSION TYPE:\s*([^\r\n]+)", "form")
    filed = datetime.strptime(header_value(r"^\s*FILED AS OF DATE:\s*([0-9]{8})", "filing date"), "%Y%m%d").date()
    if accession != index_row["accession"] or form != index_row["form"] or str(filed) != index_row["filing_date"]:
        raise ValueError("SEC 文件头与索引身份不一致")
    if form not in {"8-K", "8-K/A"} or ACCESSION.fullmatch(accession) is None:
        raise ValueError("只支持合法 8-K / 8-K/A 文件")
    header_ciks = {cik_id(value) for value in re.findall(r"CENTRAL INDEX KEY:\s*([0-9]+)", header, re.I)}
    if index_row["cik"] not in header_ciks:
        raise ValueError("SEC 文件缺少索引所指的 CIK")
    accepted_raw = header_value(r"<ACCEPTANCE-DATETIME>\s*([0-9]{14})", "acceptance time")
    local = datetime.strptime(accepted_raw, "%Y%m%d%H%M%S")
    accepted = local.replace(tzinfo=EASTERN)
    if accepted.astimezone(UTC).astimezone(EASTERN).replace(tzinfo=None) != local or accepted.utcoffset() != accepted.replace(fold=1).utcoffset():
        raise ValueError("SEC 受理时间处于不存在或歧义的当地时间")
    if accepted > first_seen or filed > first_seen.astimezone(EASTERN).date():
        raise ValueError("SEC 文件时间晚于采集时间")
    changed = header_value(r"^\s*DATE AS OF CHANGE:\s*([0-9]{8})", "change date", True)
    changed = datetime.strptime(changed, "%Y%m%d").date() if changed else None
    if changed is not None and changed > first_seen.astimezone(EASTERN).date():
        raise ValueError("文件修订日期晚于采集时间")
    period = header_value(r"^\s*CONFORMED PERIOD OF REPORT:\s*([0-9]{8})", "report date", True)
    period = datetime.strptime(period, "%Y%m%d").date() if period else None
    documents = []
    for block in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", text, re.I | re.S):
        kind = re.search(r"<TYPE>\s*([^\r\n<]+)", block, re.I)
        if kind and kind[1].strip() == form:
            filename = re.search(r"<FILENAME>\s*([^\r\n<]+)", block, re.I)
            body = re.search(r"<TEXT>(.*?)</TEXT>", block, re.I | re.S)
            if filename is None or body is None:
                raise ValueError("SEC 主文档缺少文件名或正文")
            documents.append((filename[1].strip(), body[1]))
    if len(documents) != 1:
        raise ValueError("SEC 文件未找到唯一的 8-K 主文档")
    filename, body = documents[0]
    parser = FilingText()
    parser.feed(body)
    clean = parser.text()
    if not clean:
        raise ValueError("SEC 主文档正文为空")
    items = sorted(set(re.findall(r"\bItem\s+([1-9]\.\d{2})\b", clean, re.I)))
    return {**index_row, "accepted_at": accepted.astimezone(UTC).isoformat(), "accepted_raw": accepted_raw,
            "accepted_timezone": "America/New_York", "first_seen_at": first_seen.isoformat(),
            "public_available_at": None, "availability_evidence": None,
            "report_date": str(period) if period else None, "change_date": str(changed) if changed else None,
            "post_acceptance_changed": changed is not None and changed > filed,
            "primary_document": filename, "text": clean, "items": items,
            "items_basis": "primary_document_regex_unverified", "amends_accession": None,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(), "raw_submission": text}


class EdgarArchive:
    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    accession TEXT NOT NULL, content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                    record TEXT NOT NULL, PRIMARY KEY(accession, content_hash));
                CREATE TABLE IF NOT EXISTS captures(
                    id TEXT PRIMARY KEY, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL, path TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "edgar.sqlite", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def capture(self, url, content, fetched_at):
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / "blobs" / digest
        path.parent.mkdir(exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("SEC 原始缓存与哈希不一致") from None
        with self.connect() as db:
            db.execute("INSERT INTO captures VALUES(?,?,?,?,?)", (uuid4().hex, url, fetched_at, digest, str(path)))
        return {"source_url": url, "fetched_at": fetched_at, "sha256": digest, "path": str(path), "bytes": len(content)}

    def retain(self, record):
        with self.connect() as db:
            old = db.execute("SELECT record FROM documents WHERE accession=? AND content_hash=?", (record["accession"], record["content_hash"])).fetchone()
            if old:
                old = json.loads(old[0])
                if any(old[k] != record[k] for k in ("cik", "form", "filing_date", "source_url", "raw_submission")):
                    raise ValueError("已有 SEC 版本的身份冲突")
                return old
            db.execute("INSERT INTO documents VALUES(?,?,?,?)", (record["accession"], record["content_hash"], record["first_seen_at"], json.dumps(record, ensure_ascii=False)))
        return record

    def versions(self, accessions):
        records = []
        with self.connect() as db:
            for accession in sorted(accessions):
                records.extend(json.loads(row[0]) for row in db.execute("SELECT record FROM documents WHERE accession=? ORDER BY first_seen_at,content_hash", (accession,)))
        return records


def archive_edgar(*, directory, start, end, ciks, user_agent):
    if not isinstance(user_agent, str) or len(user_agent) > 200 or re.search(r"[\r\n]", user_agent) or not re.search(r"\S+\s+.*\S+@\S+\.\S+", user_agent):
        raise ValueError("SEC 采集须显式提供含名称和联系邮箱的 User-Agent")
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if end >= datetime.now(EASTERN).date():
        raise ValueError("季度索引按夜间更新；采集结束日必须早于今天")
    planned = quarters(start, end)
    ciks = sorted({cik_id(cik) for cik in ciks})
    if not 1 <= len(ciks) <= 20:
        raise ValueError("一次需指定 1..20 个 CIK，不自动套用当前股票池")
    store = EdgarArchive(directory)
    run = store.root / "runs" / uuid4().hex
    run.mkdir(parents=True)
    report = {"schema_version": 1, "started_at": datetime.now(UTC).isoformat(), "start": str(start), "end": str(end),
              "ciks": ciks, "quarters": planned, "status": "running", "captures": [], "errors": [], "filings": 0}
    last_request = 0.0

    def fetch(client, url, maximum):
        nonlocal last_request
        time.sleep(max(0, 0.2 - (time.monotonic() - last_request)))
        last_request = time.monotonic()
        content = bytearray()
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > maximum:
                    raise ValueError("SEC 响应超过有界采集大小")
        at = datetime.now(UTC).isoformat()
        capture = store.capture(url, bytes(content), at)
        report["captures"].append(capture)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("cp1252")
        return text, at

    with runtime_lock(store.root):
        try:
            with httpx.Client(headers={"User-Agent": user_agent}, timeout=20, follow_redirects=False) as client:
                rows = {}
                for quarter in planned:
                    text, _ = fetch(client, quarter["url"], 32_000_000)
                    for row in parse_master(text, start, end, set(ciks)):
                        if row["accession"] in rows and rows[row["accession"]] != row:
                            raise ValueError("跨季度 accession 元数据冲突")
                        rows[row["accession"]] = row
                if len(rows) > 200:
                    raise ValueError("命中文件超过 200，请缩小区间；不会静默截断")
                for row in sorted(rows.values(), key=lambda r: (r["filing_date"], r["accession"])):
                    try:
                        text, at = fetch(client, row["source_url"], 8_000_000)
                        store.retain(parse_submission(text, row, at))
                        report["filings"] += 1
                    except (ValueError, httpx.HTTPError, UnicodeError) as exc:
                        report["errors"].append({"accession": row["accession"], "error": str(exc)})
                bundle = {"schema_version": 1, "data_kind": "historical_unverified", "source": "SEC quarterly master indexes and complete submissions",
                          "captured_at": datetime.now(UTC).isoformat(), "coverage": {"start": str(start), "end": str(end), "ciks": ciks,
                              "quarters": planned, "complete": not report["errors"], "universe_basis": "explicit_ciks_not_historical_population"},
                          "filings": store.versions(rows)}
                encoded = json.dumps(bundle, ensure_ascii=False, indent=2)
                if len(encoded.encode()) > 64_000_000:
                    raise ValueError("事件导出超过 64 MB，请缩小区间；原始缓存已保留")
                (run / "bundle.json").write_text(encoded + "\n")
                report["bundle_path"] = str(run / "bundle.json")
                report["status"] = "failed" if report["errors"] else "completed"
        except (ValueError, OSError, httpx.HTTPError, UnicodeError) as exc:
            report["status"] = "failed"
            report["errors"].append({"error": str(exc)})
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            (run / "capture.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return {**report, "report_path": str(run / "capture.json")}
