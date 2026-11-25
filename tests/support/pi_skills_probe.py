"""Exercise the Pi SDK tool boundary for bound skills, personal TG drafts and independent samples."""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

from tests.support.business_draft_acceptance import OWNER
from tests.support.pi_governed_probe import BrowserModel, exercise
from tests.support.pi_notification_failure import pair_before_run

SAMPLE = "合成样本：修复功能已完成。"
PROPOSAL = {"name":"合成个人周报", "description":"按用户材料整理周报",
            "selection":{"use_when":"整理周报","not_for":"审批","output":"完成事项和待办"},
            "instructions":"只按材料整理，缺失信息写为未知。"}


class Model(BrowserModel):
    def __init__(self, central):
        super().__init__(central)
        self.operation = None
        self.value = None
        self.samples = 0

    def reply(self, data):
        if not data.get("tools"):
            assert [m["role"] for m in data["messages"]] == ["system","user"]
            self.samples += 1
            return "合成完成事项。", None
        if self.operation is None:
            return super().reply(data)
        names = {t["function"]["name"] for t in data["tools"]}
        tools = [m for m in data["messages"] if m["role"] == "tool"]
        if not tools:
            assert "agentbridge_skill_authoring" in names
            return None, {"id":"draft-" + uuid4().hex, "type":"function","function":{
                "name":"agentbridge_skill_authoring", "arguments":json.dumps(self.operation,ensure_ascii=False)}}
        assert names == {"agentbridge_skill_authoring"}
        content = tools[-1]["content"]
        if isinstance(content,list):
            content = "".join(p.get("text","") for p in content)
        try:
            self.value = json.loads(content)
        except ValueError:
            self.value = {"rejected": True}
        return "本次个人草稿操作已返回。", None


def chat(post, binding, message):
    args = {"binding":binding, "requestKey":"skill-" + uuid4().hex}
    assert post("accept", **args, message=message).status_code == 200
    for _ in range(250):
        row = post("lookup", **args).json()
        if row["state"] != "accepted":
            break
        time.sleep(.1)
    assert row["state"] == "completed", (row["state"],message)
    return row["answer"]


class Scenario:
    model = Model

    def check(self, central, post, model, accounts):
        directory = central.service.workspace.hosts
        account = central.matrix.account["account_id"]
        directory.set_default(account, "openclaw-default", expected_revision=directory.choices(account)["revision"])
        assert "已加载" in chat(post, accounts["a"], "请加载工作日志助手。")
        assert "没有获配" in chat(post, accounts["b"], "请加载工作日志助手。")
        pair_before_run(central, post, accounts["a"])
        tg = post("telegram-route", botId="8801", senderId="501").json()["binding"]
        assert post("telegram-route", botId="8801", senderId="503").status_code == 403

        def author(action, data):
            model.operation = {"action":action,"data":data}
            model.value = None
            chat(post, tg, "请执行本人草稿操作" + action)
            assert isinstance(model.value, dict), model.failures
            return model.value

        draft = author("save", {"proposal":PROPOSAL})
        assert draft.get("draft_id"), list(draft)
        target = {"draft_id":draft["draft_id"], "expected_revision":draft["revision"]}
        sample = {**target, "profile":"use", "prompt":SAMPLE}
        assert author("sample",sample)["status"] == "reported" and model.samples == 1
        case = {"profile":"use","kind":"output","prompt":SAMPLE,"contains":["完成"]}
        job = author("evaluate", {**target,"cases":[case]})
        assert job["state"] == "queued"
        for _ in range(8):
            observed = author("jobs",{"job_id":job["job_id"]})
            if observed["state"] not in {"queued", "running"}:
                break
            time.sleep(.4)
        assert observed["state"] == "succeeded", observed["state"]
        inspected = author("inspect", {"draft_id":draft["draft_id"]})
        assert inspected["publication_evidence"]["report_job_id"] == job["job_id"]
        submitted = author("submit", {**target,"reason":"本人要求送审", "expected_report_job_id":job["job_id"]})
        assert submitted["published"] is False and submitted["status"] == "submitted"
        assert central.service.skill_authoring.review_detail(submitted["request_id"])["audience"] == [OWNER]
        updated = author("save",{**target,"proposal":{**PROPOSAL,"name":"合成修订周报"}})
        assert updated["revision"] == 2 and author("sample",sample).get("rejected")
        assert not model.failures
        return {"boundRevision":True,"crossSubjectDenied":True,"noToolsModelCalls":model.samples,
                "draftRevision":2,"staleSampleRefused":True,"adminReview":"pending","published":False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path,required=True)
    parser.add_argument("--sdk-dir",type=Path,required=True)
    parser.add_argument("--engine-python",type=Path)
    args = parser.parse_args()
    print(json.dumps(exercise(args.node, args.sdk_dir, args.engine_python, "skills", scenario=Scenario())),flush=True)
