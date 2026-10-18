from __future__ import annotations

import pytest
from helpers import EXAMPLE, NOW, brief, seller_quotes

from resourcepilot.bundles import plan_bundles, planner_handler
from resourcepilot.logistics import analysis_handler
from resourcepilot.pricing import booking_fee_cents, totals, usd
from resourcepilot.store import MemoryStore
from resourcepilot.vetting import verification_handler
from resourcepilot.workflow import accept_result, begin_workflow, execute_specialist, task_result

CONTEXT = {"now": NOW.isoformat()}


async def analyzed(selected: dict, addresses: dict[str, str]) -> dict:
    vetted = verification_handler(
        {"brief": selected, "quotes": await seller_quotes(selected, addresses)}, {}
    )
    return analysis_handler(vetted, CONTEXT)


def test_booking_fee_is_percent_plus_fixed(monkeypatch) -> None:
    assert booking_fee_cents(3200) == 126
    assert booking_fee_cents(0) == 0
    assert totals(3200) == {"subtotal_cents": 3200, "fee_cents": 126, "total_cents": 3326}
    monkeypatch.setenv("PLATFORM_FEE_BPS", "0")
    monkeypatch.setenv("PLATFORM_FEE_FIXED_CENTS", "0")
    assert booking_fee_cents(3200) == 0
    assert usd(3326) == "$33.26"


async def test_example_request_yields_a_budget_safe_three_seller_bundle(registry_env) -> None:
    payload = await analyzed(brief(), registry_env)
    bundles, uncovered = plan_bundles(payload["brief"], payload["offers"])
    assert uncovered == []
    best = bundles[0]
    assert best["label"] == "Lowest price"
    assert best["total_cents"] <= 3500
    assert {line["seller_id"] for line in best["lines"]} == {
        "gear_share",
        "studio_rooms",
        "westwood_print",
    }
    assert best["seller_count"] == 3
    assert best["substitutions"] == ["Mirrorless camera kit instead of a DSLR"]
    assert best["deposit_cents"] == 4000
    assert all(bundle["total_cents"] <= 3500 for bundle in bundles)
    assert len({bundle["bundle_id"] for bundle in bundles}) == len(bundles)


async def test_excluding_a_failed_seller_reroutes_to_the_backup(registry_env) -> None:
    payload = await analyzed(brief(), registry_env)
    bundles, _ = plan_bundles(payload["brief"], payload["offers"], {"westwood_print"})
    sellers = {line["seller_id"] for line in bundles[0]["lines"]}
    assert "westwood_print" not in sellers
    assert "gayley_copy" in sellers
    assert bundles[0]["total_cents"] <= 3500


async def test_priority_changes_the_recommendation(registry_env) -> None:
    payload = await analyzed(brief(priority="closest"), registry_env)
    bundles, _ = plan_bundles(payload["brief"], payload["offers"])
    assert bundles[0]["label"] == "Shortest walk"
    assert bundles[0]["walk_minutes"] == min(bundle["walk_minutes"] for bundle in bundles)


async def test_bundle_ids_are_stable(registry_env) -> None:
    payload = await analyzed(brief(), registry_env)
    first, _ = plan_bundles(payload["brief"], payload["offers"])
    second, _ = plan_bundles(payload["brief"], list(reversed(payload["offers"])))
    assert first[0]["bundle_id"] == second[0]["bundle_id"]


async def test_over_budget_request_explains_the_cheapest_total(registry_env) -> None:
    payload = await analyzed(brief(budget_cents=1000), registry_env)
    with pytest.raises(ValueError, match=r"cheapest trusted bundle costs \$33\.26"):
        plan_bundles(payload["brief"], payload["offers"])


async def test_planner_reports_uncovered_resources(registry_env) -> None:
    payload = await analyzed(brief(), registry_env)
    payload["offers"] = [o for o in payload["offers"] if o["category"] != "posters"]
    result = planner_handler(payload, {})
    assert result["uncovered"] == ["posters"]
    assert result["recommended_id"] == result["bundles"][0]["bundle_id"]


async def test_full_pipeline_through_correlated_results(registry_env) -> None:
    store = MemoryStore()
    task = await begin_workflow(
        store,
        requester="agent1qbuyer",
        session_id="s",
        request={"service": "bundle", "fields": EXAMPLE, "now": NOW.isoformat()},
    )
    visited = []
    state: dict = {}
    while task is not None:
        visited.append(task.target_role)
        if task.target_role == "discovery":
            quotes = await seller_quotes(task.payload["brief"], registry_env)
            result = task_result(
                task, "discovery", ok=True, payload={**task.payload, "quotes": quotes}
            )
        else:
            result = execute_specialist(task.target_role, task, CONTEXT)
        assert result.ok, result.error
        task, state = await accept_result(store, result)
    assert visited == ["intake", "discovery", "verification", "analysis", "planner"]
    assert state["status"] == "awaiting_selection"
    assert state["data"]["recommended_id"].startswith("bnd_")
    assert any(r["seller_id"] == "flash_prints" for r in state["data"]["rejections"])
