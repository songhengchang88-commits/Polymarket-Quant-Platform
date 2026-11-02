from pmq.journal import SCHEMA_VERSION, Journal


def test_append_and_read_roundtrip(tmp_path):
    j = Journal(str(tmp_path / "e.jsonl"), fsync=False)
    rec = j.append({"type": "TEST", "value": 1})
    assert rec["schema"] == SCHEMA_VERSION
    assert rec["id"]
    rows = j.read_all()
    assert len(rows) == 1
    assert rows[0]["value"] == 1


def test_tolerates_truncated_line(tmp_path):
    path = tmp_path / "e.jsonl"
    j = Journal(str(path), fsync=False)
    j.append({"type": "A"})
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"type": "B", "broken": ')  # 模拟进程被杀导致的半行
    rows = j.read_all()
    assert [r["type"] for r in rows] == ["A"]
