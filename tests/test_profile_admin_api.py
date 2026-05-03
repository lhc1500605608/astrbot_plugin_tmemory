"""Tests for profile admin API: summary, items, evidence, update, archive, merge.

Covers TMEAAA-285 acceptance criteria:
- Profile summary and facet-type filtered item listings
- Evidence chain retrieval
- Manual correction: update, archive, merge profile items
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer


# ═══════════════════════════════════════════════════════════════════════════════
# Profile summary
# ═══════════════════════════════════════════════════════════════════════════════


def test_get_profile_summary_empty_user(admin_svc):
    """get_profile_summary returns empty structure for unknown user."""
    result = admin_svc.get_profile_summary("nonexistent")
    assert result["user_profile"] is None
    assert result["total_items"] == 0
    assert result["facet_counts"] == {}


def test_get_profile_summary_with_items(admin_svc, seeded_profile_items):
    """get_profile_summary returns user_profile + counts after items are seeded."""
    result = admin_svc.get_profile_summary("test-user")
    assert result["user_profile"] is not None
    assert result["user_profile"]["canonical_user_id"] == "test-user"
    assert result["total_items"] == 4
    assert result["facet_counts"].get("preference") == 2
    assert result["facet_counts"].get("fact") == 1
    assert result["status_counts"].get("active") >= 3


# ═══════════════════════════════════════════════════════════════════════════════
# Profile items listing
# ═══════════════════════════════════════════════════════════════════════════════


def test_get_profile_items_all_active(admin_svc, seeded_profile_items):
    """get_profile_items returns active items sorted by importance desc."""
    items = admin_svc.get_profile_items("test-user")
    assert len(items) == 3  # 4 seeded, 1 archived
    for i in range(len(items) - 1):
        assert items[i]["importance"] >= items[i + 1]["importance"]


def test_get_profile_items_facet_filter(admin_svc, seeded_profile_items):
    """get_profile_items with facet_type filter returns only matching items."""
    items = admin_svc.get_profile_items("test-user", facet_type="preference")
    assert len(items) == 2
    for item in items:
        assert item["facet_type"] == "preference"


def test_get_profile_items_status_filter(admin_svc, seeded_profile_items):
    """get_profile_items with status filter returns only matching status."""
    items = admin_svc.get_profile_items("test-user", status="archived")
    assert len(items) == 1
    assert items[0]["status"] == "archived"


def test_get_profile_items_has_evidence_count(admin_svc, seeded_profile_items):
    """Items should include evidence_count field."""
    items = admin_svc.get_profile_items("test-user")
    assert all("evidence_count" in item for item in items)


# ═══════════════════════════════════════════════════════════════════════════════
# Profile item evidence
# ═══════════════════════════════════════════════════════════════════════════════


def test_get_profile_item_evidence(admin_svc, seeded_profile_items_with_evidence):
    """get_profile_item_evidence returns evidence chain for an item."""
    item_id = seeded_profile_items_with_evidence
    evidence = admin_svc.get_profile_item_evidence(item_id)
    assert len(evidence) >= 1
    ev = evidence[0]
    assert "source_role" in ev
    assert "evidence_kind" in ev


def test_get_profile_item_evidence_empty(admin_svc, seeded_profile_items):
    """get_profile_item_evidence returns empty list for item without evidence."""
    items = admin_svc.get_profile_items("test-user")
    item_id = items[0]["id"]
    evidence = admin_svc.get_profile_item_evidence(item_id)
    assert isinstance(evidence, list)


# ═══════════════════════════════════════════════════════════════════════════════
# Update profile item
# ═══════════════════════════════════════════════════════════════════════════════


def test_update_profile_item_content(admin_svc, seeded_profile_items):
    """update_profile_item should update content and recalc normalized_content."""
    items = admin_svc.get_profile_items("test-user")
    item_id = items[0]["id"]

    admin_svc.update_profile_item(item_id, {
        "content": "updated content here",
        "user": "test-user",
    })

    items_after = admin_svc.get_profile_items("test-user")
    updated = next(i for i in items_after if i["id"] == item_id)
    assert updated["content"] == "updated content here"


def test_update_profile_item_scores(admin_svc, seeded_profile_items):
    """update_profile_item should update confidence and importance."""
    items = admin_svc.get_profile_items("test-user")
    item_id = items[0]["id"]

    admin_svc.update_profile_item(item_id, {
        "confidence": 0.95,
        "importance": 0.88,
        "user": "test-user",
    })

    items_after = admin_svc.get_profile_items("test-user")
    updated = next(i for i in items_after if i["id"] == item_id)
    assert updated["confidence"] == 0.95
    assert updated["importance"] == 0.88


def test_update_profile_item_no_fields_raises(admin_svc, seeded_profile_items):
    """update_profile_item with no valid fields should raise ValueError."""
    items = admin_svc.get_profile_items("test-user")
    with pytest.raises(ValueError, match="no fields"):
        admin_svc.update_profile_item(items[0]["id"], {})


# ═══════════════════════════════════════════════════════════════════════════════
# Archive profile item
# ═══════════════════════════════════════════════════════════════════════════════


def test_archive_profile_item(admin_svc, seeded_profile_items):
    """archive_profile_item should change status to archived."""
    items = admin_svc.get_profile_items("test-user")
    item_id = items[0]["id"]

    result = admin_svc.archive_profile_item(item_id)
    assert result is True

    # Item should now show under archived status
    archived = admin_svc.get_profile_items("test-user", status="archived")
    assert any(i["id"] == item_id for i in archived)


# ═══════════════════════════════════════════════════════════════════════════════
# Merge profile items
# ═══════════════════════════════════════════════════════════════════════════════


def test_merge_profile_items(admin_svc, seeded_profile_items):
    """merge_profile_items should merge two items and mark the merged one as superseded."""
    items = admin_svc.get_profile_items("test-user", facet_type="preference")
    assert len(items) >= 2
    ids = [items[0]["id"], items[1]["id"]]

    result = admin_svc.merge_profile_items("test-user", ids)
    assert result["keep_id"] == ids[0]
    assert result["archived_count"] == 1

    # Merged item should be superseded (consistent with ProfileItemOps.supersede_item)
    superseded = admin_svc.get_profile_items("test-user", status="superseded")
    assert any(i["id"] == ids[1] for i in superseded)


def test_merge_profile_items_needs_min2(admin_svc, seeded_profile_items):
    """merge_profile_items with fewer than 2 ids should raise ValueError."""
    items = admin_svc.get_profile_items("test-user")
    with pytest.raises(ValueError, match="at least 2"):
        admin_svc.merge_profile_items("test-user", [items[0]["id"]])


def test_merge_profile_items_cross_facet_rejected(admin_svc, seeded_profile_items):
    """merge_profile_items should reject merging items with different facet_types."""
    prefs = admin_svc.get_profile_items("test-user", facet_type="preference")
    facts = admin_svc.get_profile_items("test-user", facet_type="fact")
    assert len(prefs) >= 1 and len(facts) >= 1
    ids = [prefs[0]["id"], facts[0]["id"]]
    with pytest.raises(ValueError, match="different facet types"):
        admin_svc.merge_profile_items("test-user", ids)


def test_merge_profile_items_keeper_is_first_id(admin_svc, seeded_profile_items):
    """merge_profile_items keeper should be ids[0], not dependent on SQLite return order."""
    items = admin_svc.get_profile_items("test-user", facet_type="preference")
    assert len(items) >= 2
    # Request ids in reverse order — keeper must still be ids[0]
    ids = [items[1]["id"], items[0]["id"]]
    result = admin_svc.merge_profile_items("test-user", ids)
    assert result["keep_id"] == ids[0]
    # The item that was ids[1] becomes superseded
    superseded = admin_svc.get_profile_items("test-user", status="superseded")
    assert any(i["id"] == ids[1] for i in superseded)


def test_merge_profile_items_relation_matches_supersede_semantics(admin_svc, plugin, seeded_profile_items):
    """Merge should create supersedes relation consistent with ProfileItemOps.supersede_item()."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps
    ops = ProfileItemOps(plugin)
    items = admin_svc.get_profile_items("test-user", facet_type="preference")
    assert len(items) >= 2
    ids = [items[0]["id"], items[1]["id"]]
    result = admin_svc.merge_profile_items("test-user", ids)
    keep_id = result["keep_id"]
    merged_id = ids[1]

    # Verify merged item status is 'superseded' (not 'archived')
    merged = admin_svc.get_profile_items("test-user", status="superseded")
    superseded_item = next(i for i in merged if i["id"] == merged_id)
    assert superseded_item["status"] == "superseded"

    # Verify relation: from_item_id=keep_id, to_item_id=merged_id, relation_type='supersedes'
    with plugin._db() as conn:
        rel = conn.execute(
            "SELECT * FROM profile_relations WHERE from_item_id=? AND to_item_id=? AND relation_type='supersedes'",
            (keep_id, merged_id),
        ).fetchone()
        assert rel is not None
        assert rel["status"] == "active"


# ═══════════════════════════════════════════════════════════════════════════════
# Global stats includes profile metrics
# ═══════════════════════════════════════════════════════════════════════════════


def test_global_stats_has_profile_counts(admin_svc, seeded_profile_items):
    """get_global_stats should include profile_user_count and profile_item_count."""
    stats = admin_svc.get_global_stats()
    assert "profile_user_count" in stats
    assert "profile_item_count" in stats
    assert stats["profile_user_count"] >= 0
    assert stats["profile_item_count"] >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# Web route coverage
# ═══════════════════════════════════════════════════════════════════════════════


async def _profile_api_client(web_module, plugin):
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_username": "admin",
            "webui_password": "secret",
        },
    )
    server._app = web_module.web.Application(middlewares=[server._middleware])
    server._setup_routes()
    client = TestClient(TestServer(server._app))
    await client.start_server()
    token = web_module.jwt_encode({"user": "admin"}, server._jwt_secret, 3600)
    return client, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_profile_summary_route_requires_auth_and_returns_counts(
    web_module, plugin, seeded_profile_items
):
    client, headers = await _profile_api_client(web_module, plugin)
    try:
        unauthenticated = await client.get("/api/profile/summary?user=test-user")
        assert unauthenticated.status == 401

        response = await client.get("/api/profile/summary?user=test-user", headers=headers)
        assert response.status == 200
        body = await response.json()
        assert body["user_profile"]["canonical_user_id"] == "test-user"
        assert body["total_items"] == 4
        assert body["facet_counts"]["preference"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_items_route_applies_facet_and_status_filters(
    web_module, plugin, seeded_profile_items
):
    client, headers = await _profile_api_client(web_module, plugin)
    try:
        response = await client.get(
            "/api/profile/items?user=test-user&facet_type=preference&status=active",
            headers=headers,
        )
        assert response.status == 200
        body = await response.json()
        assert len(body["items"]) == 2
        assert {item["facet_type"] for item in body["items"]} == {"preference"}
        assert {item["status"] for item in body["items"]} == {"active"}
        assert all("evidence_count" in item for item in body["items"])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_item_evidence_route_returns_source_chain(
    web_module, plugin, seeded_profile_items_with_evidence
):
    client, headers = await _profile_api_client(web_module, plugin)
    try:
        response = await client.get(
            f"/api/profile/items/{seeded_profile_items_with_evidence}/evidence",
            headers=headers,
        )
        assert response.status == 200
        body = await response.json()
        assert len(body["evidence"]) >= 1
        assert body["evidence"][0]["source_role"] == "user"
        assert body["evidence"][0]["evidence_kind"] == "conversation"

        missing_id = await client.get("/api/profile/items/0/evidence", headers=headers)
        assert missing_id.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_update_route_writes_changes_and_validates_id(
    web_module, plugin, admin_svc, seeded_profile_items
):
    item_id = admin_svc.get_profile_items("test-user")[0]["id"]
    client, headers = await _profile_api_client(web_module, plugin)
    try:
        bad_response = await client.post(
            "/api/profile/item/update",
            headers=headers,
            json={"content": "missing id"},
        )
        assert bad_response.status == 400

        response = await client.post(
            "/api/profile/item/update",
            headers=headers,
            json={
                "id": item_id,
                "user": "test-user",
                "content": "route updated profile content",
                "confidence": 0.77,
                "importance": 0.66,
            },
        )
        assert response.status == 200
        assert (await response.json())["ok"] is True

        updated = next(
            item for item in admin_svc.get_profile_items("test-user") if item["id"] == item_id
        )
        assert updated["content"] == "route updated profile content"
        assert updated["confidence"] == 0.77
        assert updated["importance"] == 0.66
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_archive_route_moves_item_to_archived_status(
    web_module, plugin, admin_svc, seeded_profile_items
):
    item_id = admin_svc.get_profile_items("test-user")[0]["id"]
    client, headers = await _profile_api_client(web_module, plugin)
    try:
        response = await client.post(
            "/api/profile/item/archive",
            headers=headers,
            json={"id": item_id},
        )
        assert response.status == 200
        assert (await response.json()) == {"ok": True}
        assert any(
            item["id"] == item_id
            for item in admin_svc.get_profile_items("test-user", status="archived")
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_merge_route_supersedes_secondary_items(
    web_module, plugin, admin_svc, seeded_profile_items
):
    items = admin_svc.get_profile_items("test-user", facet_type="preference")
    ids = [items[0]["id"], items[1]["id"]]
    client, headers = await _profile_api_client(web_module, plugin)
    try:
        invalid = await client.post(
            "/api/profile/items/merge",
            headers=headers,
            json={"user": "test-user", "ids": ids[:1]},
        )
        assert invalid.status == 400

        response = await client.post(
            "/api/profile/items/merge",
            headers=headers,
            json={"user": "test-user", "ids": ids},
        )
        assert response.status == 200
        body = await response.json()
        assert body["ok"] is True
        assert body["keep_id"] == ids[0]
        assert body["archived_count"] == 1
        assert any(
            item["id"] == ids[1]
            for item in admin_svc.get_profile_items("test-user", status="superseded")
        )
    finally:
        await client.close()
