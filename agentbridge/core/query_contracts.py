from __future__ import annotations


HISTORY_QUERY_EVIDENCE_SCHEMA = {
    "serverFilterApplied": {"type": "boolean"},
    "sourceQueryTotal": {"type": ["integer", "null"]},
    "sourceQueryPages": {"type": ["integer", "null"]},
    "pagesFetched": {"type": "integer"},
    "scanBudget": {"type": "integer"},
}


def oa_history_query_contract(collection: str) -> dict:
    if collection not in {"done", "sent"}:
        raise ValueError("history query contract only supports done and sent")
    return {
        "schemaVersion": "agentbridge.query-contract.v1",
        "dateBasis": "processed_at" if collection == "done" else "initiated_at",
        "timeZone": "Asia/Shanghai",
        "filters": {
            "dateRange": {
                "arguments": ["start_date", "end_date"],
                "format": "YYYY-MM-DD",
                "bounds": "inclusive",
                "openBoundsSupported": True,
                "execution": "server",
            },
            "keyword": {
                "argument": "keyword", "execution": "client_after_date_filter",
                "matches": "public_item_fields_case_insensitive",
            },
        },
        "pagination": {
            "mode": "automatic_filtered_pages", "pageSize": 50,
            "maxScannedRows": 1000, "timeBudgetSeconds": 20,
            "limitArgument": "limit", "maximumLimit": 1000,
            "withoutDateRange": "loaded_page_only",
        },
        "ordering": "source_order_not_a_proof_of_completeness",
        "completeness": {
            "pointer": "/coverage", "requiredForDerivedWrite": "complete",
            "proof": "all_filtered_pages_validated_and_no_output_truncation",
            "fallbackToUnfilteredScan": False,
        },
    }
