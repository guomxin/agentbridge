"""Verified, read-only business-settlement form at the reimbursement approval node."""
from copy import deepcopy
from decimal import Decimal
import re

TEMPLATE_ID = "8886783585203274099"
FORM_APP_ID = "-1826745395617707257"
FIELD_LABELS = {
    "field0001_id": "流水号", "field0002_id": "姓名", "field0003_id": "工号",
    "field0004_id": "发起部门", "field0005_id": "日期", "field0006_id": "结算方式",
    "field0007_id": "费用归属部门", "field0032_id": "结算实体", "field0067_id": "费用归属事项",
    "field0068_id": "财务编号", "field0008_id": "关联流程", "field0009_id": "关联文件",
    "field0013_id": "明细日期", "field0010_id": "事项说明", "field0012_id": "费用小类",
    "field0057_id": "费用子类", "field0011_id": "发票费用名称", "field0015_id": "金额（元）",
    "field0016_id": "稽核差额（元）", "field0017_id": "电子发票及凭证",
    "field0022_id": "应付金额合计", "field0023_id": "应付金额大写", "field0036_id": "稽核会计",
    "field0021_id": "稽核日期", "field0024_id": "账户类型", "field0025_id": "收款账户名称",
    "field0026_id": "收款账号", "field0027_id": "收款账户开户行", "field0028_id": "备注",
    "field0118_id": "付款方式", "field0109_id": "付款日期", "field0110_id": "付款金额（元）",
    "field0111_id": "付款登记方式", "field0116_id": "延支日期", "field0112_id": "出纳",
    "field0113_id": "付款备注", "field0029_id": "审批信息",
}
EXPENSE_FIELDS = tuple(f"field{i:04d}_id" for i in (13, 10, 12, 57, 11, 15, 16, 17))
PAYMENT_FIELDS = tuple(f"field{i:04d}_id" for i in (109, 110, 111, 116, 112, 113))
MASTER_FIELDS = tuple(key for key in FIELD_LABELS if key not in EXPENSE_FIELDS + PAYMENT_FIELDS)
OPTIONAL_FIELDS = {"field0008_id", "field0009_id", "field0021_id", "field0028_id", "field0118_id", "field0029_id"}
ATTACHMENT_FIELDS = {"field0009_id", "field0017_id"}


def _amount(value):
    if not re.fullmatch(r"(?:0|[1-9]\d{0,11})(?:\.\d{1,2})?", value):
        raise ValueError("The business-settlement amount is unavailable or invalid.")
    return Decimal(value)


def business_settlement_business_snapshot(page):
    frames = [frame for frame in page.frames if "/cap4/" in str(frame.url or "")
              and frame.locator("#field0001_id").count() == 1]
    if len(frames) != 1:
        raise ValueError("The business-settlement CAP4 form is unavailable or ambiguous.")
    snapshot = frames[0].evaluate(BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT)
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise ValueError("Business-settlement fields must be read-only at the reimbursement approval node.")
    fields = snapshot.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 2000:
        raise ValueError("The business-settlement fields are unavailable or too large.")
    if any(not isinstance(field, dict) or not isinstance(field.get("id"), str) for field in fields):
        raise ValueError("The business-settlement field structure changed.")
    if {field["id"] for field in fields} != set(FIELD_LABELS):
        raise ValueError("The business-settlement field structure changed.")
    rows = {"expense": {}, "payment": {}}
    masters = {}
    for field in fields:
        value, record, key = field.get("value"), field.get("record_id"), field["id"]
        if (not isinstance(value, str) or len(value) > 10000 or not isinstance(record, str)
                or len(record) > 256 or any(ord(c) < 32 for c in record)):
            raise ValueError("The business-settlement field value or row identity is invalid.")
        if key in MASTER_FIELDS:
            if key in masters or record or (key not in OPTIONAL_FIELDS and not value.strip()):
                raise ValueError("The business-settlement master fields are incomplete or ambiguous.")
            masters[key] = value
        else:
            if not record.strip():
                raise ValueError("The business-settlement row identity is missing.")
            group = rows["expense" if key in EXPENSE_FIELDS else "payment"]
            row = group.setdefault(record, {})
            if key in row:
                raise ValueError("The business-settlement row fields are duplicated.")
            row[key] = value
        if key in ATTACHMENT_FIELDS:
            ids, count = field.get("attachment_ids"), field.get("attachment_count")
            if (not isinstance(ids, list) or type(count) is not int or count < 0 or count > 200
                    or len(ids) != count or any(not isinstance(i, str) or not re.fullmatch(r"-?\d{1,20}", i) for i in ids)
                    or len(set(ids)) != len(ids) or bool(value.strip()) != bool(count)):
                raise ValueError("The business-settlement attachment identities are incomplete or ambiguous.")
    if set(rows["expense"]) & set(rows["payment"]):
        raise ValueError("The business-settlement expense and payment row identities overlap.")
    for kind, keys in (("expense", EXPENSE_FIELDS), ("payment", PAYMENT_FIELDS)):
        if not rows[kind] or any(set(row) != set(keys) for row in rows[kind].values()):
            raise ValueError("The business-settlement rows are incomplete.")
    # These are the observed reimbursement branch and read-only account choices.
    # Advance payments, audit adjustments and recorded payments have different semantics.
    if (masters["field0006_id"] != "报销并付款"
            or masters["field0024_id"] not in {"个人账户", "他人/对公账户"}
            or masters["field0118_id"] not in {"", "一次性结算"}
            or masters["field0008_id"] or masters["field0009_id"]):
        raise ValueError("The business-settlement branch or related material is not verified.")
    if any(value.strip() for row in rows["payment"].values() for value in row.values()):
        raise ValueError("Business-settlement recorded payments require a separately verified contract.")
    total = _amount(masters["field0022_id"])
    amounts = []
    for row in rows["expense"].values():
        if any(not row[key].strip() for key in EXPENSE_FIELDS if key != "field0016_id"):
            raise ValueError("The business-settlement expense details are incomplete.")
        if row["field0016_id"] and _amount(row["field0016_id"]) != 0:
            raise ValueError("Business-settlement audit adjustments require a separately verified contract.")
        amount = _amount(row["field0015_id"])
        if amount <= 0:
            raise ValueError("The business-settlement expense amount must be positive.")
        amounts.append(amount)
    if total <= 0 or sum(amounts, Decimal(0)) != total:
        raise ValueError("The business-settlement expense details do not match the total amount.")
    return {"business_fields_browse_only": True, "fields": deepcopy(fields)}


def business_settlement_summary_fields(snapshot):
    fields = [{"label": FIELD_LABELS[field["id"]], "value": field["value"]}
              for field in snapshot["fields"] if field["id"] in MASTER_FIELDS]
    for title, keys in (("费用明细", EXPENSE_FIELDS), ("付款记录", PAYMENT_FIELDS)):
        rows = {}
        for field in snapshot["fields"]:
            if field["id"] in keys:
                rows.setdefault(field["record_id"], {})[field["id"]] = field["value"]
        for index, row in enumerate(rows.values(), 1):
            fields.extend({"label": f"{title} {index} · {FIELD_LABELS[key]}", "value": row[key]} for key in keys)
    return fields


BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT = r"""
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
    fields: elements.map(element => {
      const section = element.querySelector(':scope > section');
      let value = clean(element.querySelector('.field-content-wrapper')?.innerText);
      if (section?.classList.contains('cap4-radio')) {
        const selected = Array.from(element.querySelectorAll('.cap4-radio__item')).filter(item =>
          item.querySelector('.cap4-radio-xuanzhong, .cap-icon-danxuan-xuanzhong'));
        value = selected.length === 0 ? '' : selected.length === 1
          ? clean(selected[0].querySelector('.cap4-radio__text')?.textContent) : '__ambiguous__';
      }
      const field = {id: element.id, value,
        record_id: String(element.closest('tr[data-record-id]')?.getAttribute('data-record-id') || '')};
      if (section?.classList.contains('cap4-attach')) {
        const links = Array.from(element.querySelectorAll('a.cap4-attach-downall'));
        const attachments = Array.from(element.querySelectorAll('.cap4-attach__att'));
        field.attachment_count = attachments.length;
        field.attachment_ids = links.flatMap(link => {
          const href = link.getAttribute('href') || '';
          const match = href.match(/^\/seeyon\/rest\/attachment\/file\/batchDownload\/(-?\d+(?:,-?\d+)*)(?:\?|$)/);
          return match ? match[1].split(',') : ['invalid'];
        });
        // CAP4 intentionally leaves its hidden batch link empty for one file.
        // Bind that file's rendered name to both read-only component records;
        // absent or conflicting metadata must still fail the identity check.
        if (attachments.length === 1 && links.length === 1
            && /^\/seeyon\/rest\/attachment\/file\/batchDownload\/(?:\?[^#]*)?$/.test(links[0].getAttribute('href') || '')
            && getComputedStyle(links[0]).display === 'none') {
          const component = section.__vue__;
          const data = component?.$props?.data;
          const values = data?.value;
          const infos = data?.attachmentInfo?.attachmentInfos;
          const item = Array.isArray(values) && values.length === 1 ? values[0] : null;
          const info = Array.isArray(infos) && infos.length === 1 ? infos[0] : null;
          const numericId = value => typeof value === 'string' && /^-?\d{1,20}$/.test(value);
          const rendered = clean(attachments[0].querySelector('.cap4-attach__aright')?.innerText);
          const namedSize = rendered.match(/^(.*) \(\d+(?:\.\d+)?(?:B|KB|MB|GB)\)$/);
          if (component?.$options?.name === 'Cap4Attachment' && component.$el === section
              && data?.auth === 'browse' && data.type === 'attachment' && data.ctrlType === 'suiAttachment'
              && data.id === element.id.replace(/_id$/, '') && data.recordId === field.record_id
              && data.formId === '-1826745395617707257'
              && item && info && numericId(item.fileUrl) && item.fileUrl === info.fileUrl
              && typeof item.name === 'string' && clean(item.name) && item.name === info.filename
              && namedSize?.[1] === clean(item.name) && rendered === value
              && typeof item.size === 'string' && /^[1-9]\d{0,19}$/.test(item.size) && item.size === info.size
              && numericId(data.showValue) && data.showValue === data.showValue2
              && data.showValue === data.attachmentInfo.baseAttachmentInfo?.subReference
              && data.showValue === info.subReference) {
            field.attachment_ids = [item.fileUrl];
          }
        }
      }
      return field;
    }),
  };
}
"""
