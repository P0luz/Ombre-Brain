"""客户端契约测试：各家钩子的真实输入样例（tests/fixtures/autorecall_clients/<客户端>/cases.json）喂给 ob_hook.py。

样例来源写在每份 cases.json 的 sources 里：官方文档示例原样照抄、官方 schema、或能用的开源实现。
客户端改版后，重新取样例 / schema 放进来，这里会告诉你哪一条对不上。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_autorecall_gateway import FakeOB, _wait  # noqa: E402

_ROOT = Path(__file__).resolve().parent / "fixtures" / "autorecall_clients"
_HOOK = Path(__file__).resolve().parents[1] / "src" / "ombrebrain" / "autorecall" / "client" / "ob_hook.py"


def _cases():
    out = []
    for path in sorted(_ROOT.glob("*/cases.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        assert spec.get("sources"), f"{path} 没写样例来源"
        for case in spec["cases"]:
            out.append(pytest.param(path.parent, case, id=f"{spec['client']}:{case['name']}"))
    return out


def _schema(client_dir: Path, name: str) -> dict:
    return json.loads((client_dir / "schema" / f"{name}.schema.json").read_text(encoding="utf-8"))


@pytest.fixture
def ob():
    server = FakeOB()
    yield server
    server.server.shutdown()


@pytest.mark.parametrize("client_dir,case", _cases())
def test_client_contract(client_dir, case, ob, tmp_path):
    stdin = dict(case["stdin"])
    if "schema" in case:
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(stdin, _schema(client_dir, case["schema"]))  # 样例本身符合官方 schema
    if "transcript_rows" in case:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in case["transcript_rows"]),
                              encoding="utf-8")
        stdin["transcriptPath"] = str(transcript)

    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "OB_URL": ob.url, "OB_HOOK_TOKEN": "tok",
           "OB_RELAY": "0", "OB_RELAY_STATE": str(tmp_path / ".ob_relay.json")}
    result = subprocess.run([sys.executable, str(_HOOK), *case["argv"]], input=json.dumps(stdin, ensure_ascii=False),
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr

    expected_out = case["expect_stdout"]
    if expected_out is None:
        assert result.stdout.strip() == ""
    else:
        produced = json.loads(result.stdout)
        assert produced == expected_out
        if "output_schema" in case:
            jsonschema = pytest.importorskip("jsonschema")
            jsonschema.validate(produced, _schema(client_dir, case["output_schema"]))  # 输出符合官方 schema

    post = case["expect_post"]
    if post is None:
        time.sleep(0.5)  # 存回复是交给后台发的，等它一会儿再确认确实没发
        assert ob.calls == []
        return
    assert _wait(lambda: any(c[0] == post["path"] for c in ob.calls), 10), f"OB 没收到 {post['path']}"
    body = [c[1] for c in ob.calls if c[0] == post["path"]][-1]
    for key, value in post.get("fields", {}).items():
        assert body.get(key) == value, f"{key}: {body.get(key)!r} != {value!r}"
    if "message" in post:
        message = body["messages"][-1]
        assert message["role"] == post["message"]["role"] and message["text"] == post["message"]["text"]
        assert message["key"].startswith(post["message"].get("key_prefix", ""))
    if "messages" in post:
        assert body["messages"] == post["messages"]
