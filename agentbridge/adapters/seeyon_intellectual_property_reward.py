"""The verified read-only inform node of the intellectual-property reward form."""

from decimal import Decimal
import re


TEMPLATE_ID = "8783733253983913697"
FORM_APP_ID = "-7252470787007887384"
FIELD_LABELS = {
    "field0001_id": "申请人", "field0002_id": "工号", "field0003_id": "所属部门",
    "field0004_id": "申请日期", "field0005_id": "奖项名称", "field0006_id": "奖励金额",
    "field0007_id": "所属业务负责人", "field0008_id": "奖励事由", "field0009_id": "团队负责人",
    "field0010_id": "团队负责人部门", "field0011_id": "奖励人姓名", "field0012_id": "奖励金额明细",
    "field0013_id": "团队成员确认", "field0014_id": "审批意见", "field0015_id": "提示信息",
}
ROW_FIELDS = ("field0011_id", "field0012_id")
MASTER_FIELDS = tuple(key for key in FIELD_LABELS if key not in ROW_FIELDS)
OPTIONAL_FIELDS = {"field0007_id", "field0013_id", "field0014_id", "field0015_id"}


def _amount(value):
    if not re.fullmatch(r"(?:0|[1-9]\d{0,11})(?:\.\d{1,2})?", value):
        raise ValueError("The intellectual-property reward amount is unavailable or invalid.")
    return Decimal(value)


def intellectual_property_reward_business_snapshot(page):
    frames = [frame for frame in page.frames if "/cap4/" in str(frame.url or "")
              and frame.locator("#field0001_id").count() == 1]
    if len(frames) != 1:
        raise ValueError("The intellectual-property reward CAP4 form is unavailable or ambiguous.")
    snapshot = frames[0].evaluate(INTELLECTUAL_PROPERTY_REWARD_SNAPSHOT_SCRIPT)
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise ValueError("Intellectual-property reward fields must be read-only at the inform node.")
    fields = snapshot.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 2000:
        raise ValueError("The intellectual-property reward fields are unavailable or too large.")
    if any(not isinstance(field, dict) for field in fields):
        raise ValueError("The intellectual-property reward field structure changed.")
    if any(not isinstance(field.get("id"), str) for field in fields) or {field["id"] for field in fields} != set(FIELD_LABELS):
        raise ValueError("The intellectual-property reward field structure changed.")
    rows = {}
    for field in fields:
        value, record = field.get("value"), field.get("record_id")
        if not isinstance(value, str) or len(value) > 10000 or not isinstance(record, str) or len(record) > 256:
            raise ValueError("The intellectual-property reward field value or row identity is invalid.")
        if any(ord(character) < 32 for character in record):
            raise ValueError("The intellectual-property reward row identity is invalid.")
        if field["id"] in ROW_FIELDS:
            if not record or not value.strip():
                raise ValueError("The intellectual-property reward allocation identity is incomplete.")
            rows.setdefault(record, []).append(field)
    values = {}
    for key in MASTER_FIELDS:
        items = [field for field in fields if field["id"] == key]
        if len(items) != 1 or items[0]["record_id"] or (key not in OPTIONAL_FIELDS and not items[0]["value"].strip()):
            raise ValueError("The intellectual-property reward master fields are incomplete or ambiguous.")
        values[key] = items[0]["value"]
    if not rows or any(sorted(field["id"] for field in row) != sorted(ROW_FIELDS) for row in rows.values()):
        raise ValueError("The intellectual-property reward allocation rows are incomplete or duplicated.")
    total = _amount(values["field0006_id"])
    allocated = sum((_amount(next(field["value"] for field in row if field["id"] == "field0012_id"))
                     for row in rows.values()), Decimal(0))
    if total <= 0 or allocated != total:
        raise ValueError("The intellectual-property reward allocation does not match the total amount.")
    return {"business_fields_browse_only": True, "fields": fields}


def intellectual_property_reward_summary_fields(snapshot):
    fields = [{"label": FIELD_LABELS[field["id"]], "value": field["value"]}
              for field in snapshot["fields"] if field["id"] in MASTER_FIELDS]
    rows = {}
    for field in snapshot["fields"]:
        if field["id"] in ROW_FIELDS:
            rows.setdefault(field["record_id"], {})[field["id"]] = field["value"]
    # Legacy authorization cards render summary fields rather than sections.
    # Keep every recipient/amount visible on that still-supported surface too.
    for index, row in enumerate(rows.values(), 1):
        fields.extend([
            {"label": f"奖励人 {index}", "value": row["field0011_id"]},
            {"label": f"奖励金额 {index}（元）", "value": row["field0012_id"]},
        ])
    return fields


INTELLECTUAL_PROPERTY_REWARD_SNAPSHOT_SCRIPT = r"""
() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const elements = Array.from(document.querySelectorAll('[id^="field"][id$="_id"]'));
  const editable = control => {
    const style = getComputedStyle(control);
    return !control.disabled && !control.readOnly && control.type !== 'hidden'
      && style.visibility !== 'hidden' && style.display !== 'none'
      && style.opacity !== '0' && control.getClientRects().length > 0;
  };
  return {
    browse_only: elements.length > 0 && elements.every(element =>
      element.querySelector(':scope > section')?.classList.contains('is-none')
      && !Array.from(element.querySelectorAll('input,textarea,select,[contenteditable="true"]')).some(editable)),
    fields: elements.map(element => ({
      id: element.id,
      value: clean(element.querySelector('.field-content-wrapper')?.innerText),
      record_id: String(element.closest('tr[data-record-id]')?.getAttribute('data-record-id') || ''),
    })),
  };
}
"""
