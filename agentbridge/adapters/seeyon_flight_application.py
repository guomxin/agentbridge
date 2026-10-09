"""Read-only CAP4 contract for approval of a flight application."""

FIELD_LABELS = {
    "field0001_id": "申请人",
    "field0002_id": "工号",
    "field0003_id": "部门",
    "field0004_id": "申请日期",
    "field0009_id": "流程处理意见",
    "field0013_id": "出发时间",
    "field0014_id": "出发地点",
    "field0015_id": "到达地点",
    "field0016_id": "申请事由",
}
LEG_FIELDS = tuple(f"field{number:04d}_id" for number in (13, 14, 15, 16))
MAIN_FIELDS = tuple(key for key in FIELD_LABELS if key not in LEG_FIELDS)


def flight_application_business_snapshot(page) -> dict:
    frames = [
        frame for frame in page.frames
        if "/cap4/" in str(frame.url or "")
        and frame.locator("#field0001_id").count() == 1
    ]
    if len(frames) != 1:
        raise ValueError("The OA flight-application CAP4 form is unavailable or ambiguous.")
    snapshot = frames[0].evaluate(FLIGHT_APPLICATION_SNAPSHOT_SCRIPT)
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise ValueError("The OA flight-application fields are editable or unavailable.")
    fields = snapshot.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 205:
        raise ValueError("The OA flight-application field structure is unavailable or too large.")
    if {field.get("id") for field in fields if isinstance(field, dict)} != set(FIELD_LABELS):
        raise ValueError("The OA flight-application field structure changed.")
    main = {key: [] for key in MAIN_FIELDS}
    legs: dict[str, dict[str, dict]] = {}
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("The OA flight-application field structure changed.")
        key, label, value = field.get("id"), field.get("label"), field.get("value")
        record_id = field.get("record_id")
        if label != FIELD_LABELS.get(key) or not isinstance(value, str) or len(value) > 10000:
            raise ValueError("The OA flight-application field label or value changed.")
        if key in MAIN_FIELDS:
            if record_id:
                raise ValueError("The OA flight-application master field has a row identity.")
            main[key].append(field)
        else:
            if not isinstance(record_id, str) or not record_id:
                raise ValueError("The OA flight-application leg identity is unavailable.")
            row = legs.setdefault(record_id, {})
            if key in row:
                raise ValueError("The OA flight-application leg has duplicate fields.")
            row[key] = field
            if not value:
                raise ValueError("The OA flight-application leg is incomplete.")
    if any(len(items) != 1 for items in main.values()):
        raise ValueError("The OA flight-application master fields are incomplete or ambiguous.")
    if any(not main[key][0]["value"] for key in MAIN_FIELDS if key != "field0009_id"):
        raise ValueError("The OA flight-application applicant information is incomplete.")
    if not legs or len(legs) > 50 or any(set(row) != set(LEG_FIELDS) for row in legs.values()):
        raise ValueError("The OA flight-application legs are incomplete or ambiguous.")
    return {"business_fields_browse_only": True, "fields": fields}


def flight_application_summary_fields(snapshot: dict) -> list[dict]:
    result, row_numbers = [], {}
    for field in snapshot["fields"]:
        label = FIELD_LABELS[field["id"]]
        if field["id"] in LEG_FIELDS:
            ordinal = row_numbers.setdefault(field["record_id"], len(row_numbers) + 1)
            label = f"航段{ordinal} · {label}"
        if field["id"] != "field0009_id" or field["value"]:
            result.append({"label": label, "value": field["value"] or "（空）"})
    return result


FLIGHT_APPLICATION_SNAPSHOT_SCRIPT = r"""
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
      element.querySelector(':scope > section')?.classList.contains('is-none'))
      && !Array.from(document.querySelectorAll('input,textarea,select,[contenteditable="true"]'))
        .some(visibleEditable),
    fields: elements.map((element) => ({
      id: element.id,
      label: clean(element.querySelector('.label-margin')?.textContent),
      value: clean(element.querySelector('.field-content-wrapper')?.innerText),
      record_id: String(element.closest('tr[data-record-id]')?.getAttribute('data-record-id') || ''),
    })),
  };
}
"""
