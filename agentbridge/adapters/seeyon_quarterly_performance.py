"""Read-only quarterly-performance inform-node contract; no ratings are edited."""

TEMPLATE_ID = "849304960656164262"
FORM_APP_ID = "7864803665985356971"
FIELD_LABELS = {
    "field0001_id": "考评人", "field0002_id": "部门名称", "field0003_id": "复核截止日期",
    "field0015_id": "A等级占比", "field0016_id": "B等级占比", "field0025_id": "提醒",
    "field0006_id": "被考评人", "field0007_id": "岗位", "field0008_id": "一级审批人",
    "field0009_id": "二级审批人", "field0011_id": "二级评价等级",
    "field0010_id": "一级评价等级", "field0017_id": "一级评语", "field0012_id": "二级评语",
    "field0022_id": "部门评级", "field0023_id": "部门调整理由",
}
ROW_FIELDS = tuple(f"field{n:04d}_id" for n in (6, 7, 8, 9, 10, 17, 11, 12, 22, 23))
MASTER_FIELDS = tuple(key for key in FIELD_LABELS if key not in ROW_FIELDS)


def quarterly_performance_business_snapshot(page):
    frames = [f for f in page.frames if "/cap4/" in str(f.url or "")
              and f.locator("#field0001_id").count() == 1]
    if len(frames) != 1:
        raise ValueError("The quarterly-performance CAP4 form is unavailable or ambiguous.")
    snapshot = frames[0].evaluate(QUARTERLY_PERFORMANCE_SNAPSHOT_SCRIPT)
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise ValueError("Quarterly-performance fields must be read-only at the inform node.")
    fields = snapshot.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 2000:
        raise ValueError("The quarterly-performance field structure is unavailable or too large.")
    if any(not isinstance(f, dict) for f in fields) or {f.get("id") for f in fields} != set(FIELD_LABELS):
        raise ValueError("The quarterly-performance field structure changed.")
    rows = {}
    for key in MASTER_FIELDS:
        values = [f for f in fields if f["id"] == key]
        if len(values) != 1 or values[0].get("record_id") or (key != "field0025_id" and not values[0].get("value")):
            raise ValueError("The quarterly-performance master fields are incomplete or ambiguous.")
    for field in fields:
        if not isinstance(field.get("value"), str) or len(field["value"]) > 10000:
            raise ValueError("The quarterly-performance field value is invalid or too large.")
        if field["id"] in ROW_FIELDS:
            record = field.get("record_id")
            if not isinstance(record, str) or not record:
                raise ValueError("The quarterly-performance employee row identity is unavailable.")
            rows.setdefault(record, []).append(field)
    if not rows or any(sorted(f["id"] for f in row) != sorted(ROW_FIELDS) for row in rows.values()):
        raise ValueError("The quarterly-performance employee rows are incomplete or duplicated.")
    for row in rows.values():
        values = {f["id"]: f["value"] for f in row}
        if not values["field0006_id"] or not values["field0007_id"] or not values["field0008_id"]:
            raise ValueError("The quarterly-performance employee identity is incomplete.")
        if any(values[k] not in {"A", "B", "C", "D"} for k in ("field0010_id", "field0022_id")):
            raise ValueError("The quarterly-performance rating is unavailable or unsupported.")
        if values["field0011_id"] not in {"", "A", "B", "C", "D"}:
            raise ValueError("The quarterly-performance secondary rating is unsupported.")
    return {"business_fields_browse_only": True, "fields": fields}


def quarterly_performance_summary_fields(snapshot):
    return [{"label": FIELD_LABELS[f["id"]], "value": f["value"]}
            for f in snapshot["fields"] if f["id"] in MASTER_FIELDS]


QUARTERLY_PERFORMANCE_SNAPSHOT_SCRIPT = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const elements = Array.from(document.querySelectorAll('[id^="field"][id$="_id"]'));
  const visibleEditable = (control) => {
    const style = getComputedStyle(control);
    return !control.disabled && !control.readOnly && control.type !== 'hidden'
      && style.visibility !== 'hidden' && style.display !== 'none'
      && style.opacity !== '0' && control.getClientRects().length > 0;
  };
  return {
    browse_only: elements.length > 0 && elements.every((element) =>
      element.querySelector(':scope > section')?.classList.contains('is-none')
      && !Array.from(element.querySelectorAll('input,textarea,select,[contenteditable="true"]'))
        .some(visibleEditable)),
    fields: elements.map((element) => {
      let value = clean(element.querySelector('.field-content-wrapper')?.innerText);
      return {id: element.id, value,
        record_id: String(element.closest('tr[data-record-id]')?.getAttribute('data-record-id') || '')};
    }),
  };
}
"""
