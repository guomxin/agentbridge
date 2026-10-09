"""Read-only CAP4 contract for the work-handover approval node."""

FIELD_LABELS = {
    "field0001_id": "移交人", "field0002_id": "移交人部门",
    "field0006_id": "交接日期", "field0005_id": "监交人",
    "field0008_id": "交接类别", "field0058_id": "提示",
    "field0040_id": "工作事项", "field0049_id": "完成情况、交接说明",
    "field0050_id": "相关内外联系人", "field0051_id": "工作附件",
    "field0052_id": "工作接收人", "field0053_id": "工作接收日期",
    "field0055_id": "工作接收人确认", "field0041_id": "财务交接项",
    "field0061_id": "财务交接金额", "field0062_id": "财务交接情况",
    "field0063_id": "财务接收人", "field0065_id": "财务接收日期",
    "field0066_id": "财务接收人确认", "field0073_id": "财务交接备注",
    "field0026_id": "移交/接收部门意见", "field0023_id": "监交人意见",
    "field0087_id": "接收人意见",
}
WORK_FIELDS = tuple(f"field{n:04d}_id" for n in (40, 49, 50, 51, 52, 53, 55))
FINANCE_FIELDS = tuple(f"field{n:04d}_id" for n in (41, 61, 62, 63, 65, 66))


def work_handover_business_snapshot(page) -> dict:
    frames = [frame for frame in page.frames if "/cap4/" in str(frame.url or "")
              and frame.locator("#field0001_id").count() == 1]
    if len(frames) != 1:
        raise ValueError("The OA work-handover CAP4 form is unavailable or ambiguous.")
    snapshot = frames[0].evaluate(WORK_HANDOVER_SNAPSHOT_SCRIPT)
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise ValueError("The OA work-handover fields are editable or incomplete; a separate field-entry contract is required.")
    fields = snapshot.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 1000:
        raise ValueError("The OA work-handover field structure is unavailable or exceeds the supported size.")
    if {f.get("id") for f in fields} != set(FIELD_LABELS):
        raise ValueError("The OA work-handover field structure changed.")
    by_id = {key: [f for f in fields if f["id"] == key] for key in FIELD_LABELS}
    for key in set(FIELD_LABELS) - set(WORK_FIELDS) - set(FINANCE_FIELDS):
        if len(by_id[key]) != 1:
            raise ValueError("The OA work-handover master fields are ambiguous.")
    for key in ("field0001_id", "field0002_id", "field0006_id", "field0005_id"):
        if not by_id[key][0].get("value"):
            raise ValueError(f"The OA work-handover {FIELD_LABELS[key]} is unavailable.")
    if by_id["field0008_id"][0].get("value") not in {"部门间调迁", "部门内调岗", "离职", "其他"}:
        raise ValueError("The OA work-handover selected category is unavailable.")
    for group in (WORK_FIELDS, FINANCE_FIELDS):
        rows = {}
        for field in fields:
            if field["id"] in group:
                record = field.get("record_id")
                if not record:
                    raise ValueError("The OA work-handover detail row identity is unavailable.")
                rows.setdefault(record, []).append(field["id"])
        if not rows or any(sorted(ids) != sorted(group) for ids in rows.values()):
            raise ValueError("The OA work-handover detail rows are incomplete or duplicated.")
    for field in fields:
        if not isinstance(field.get("value"), str) or len(field["value"]) > 10000:
            raise ValueError("The OA work-handover field value exceeds the supported size.")
    return {"business_fields_browse_only": True, "fields": fields}


def work_handover_summary_fields(snapshot: dict) -> list[dict]:
    result, row_numbers = [], {}
    for field in snapshot["fields"]:
        group = "工作" if field["id"] in WORK_FIELDS else "财务" if field["id"] in FINANCE_FIELDS else ""
        label = FIELD_LABELS[field["id"]]
        if group:
            rows = row_numbers.setdefault(group, {})
            ordinal = rows.setdefault(field["record_id"], len(rows) + 1)
            label = f"{group}明细{ordinal} · {label}"
        result.append({"label": label, "value": field["value"] or "（空）"})
    return result


WORK_HANDOVER_SNAPSHOT_SCRIPT = r"""
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
      if (element.id === 'field0008_id') {
        const chosen = Array.from(element.querySelectorAll('.cap4-radio__item')).filter(
          (item) => item.querySelector('.cap4-radio-xuanzhong, .cap-icon-danxuan-xuanzhong'));
        value = chosen.length === 1 ? clean(chosen[0].querySelector('.cap4-radio__text')?.textContent) : '';
      }
      return {id: element.id, value,
        record_id: String(element.closest('tr[data-record-id]')?.getAttribute('data-record-id') || '')};
    }),
  };
}
"""
