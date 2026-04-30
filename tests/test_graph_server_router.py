"""Tests for `code_index.commands.graph_server_router`."""

from __future__ import annotations

from code_index.commands.graph_server_router import Route, Router


def dummy_handler() -> str:
    return "ok"


def another_handler() -> str:
    return "another"


class TestRoute:
    def test_match_exact_path_and_method(self):
        route = Route("GET", "/api/health", dummy_handler)
        params = route.match("GET", "/api/health")
        assert params == {}

    def test_match_different_method_returns_none(self):
        route = Route("GET", "/api/health", dummy_handler)
        assert route.match("POST", "/api/health") is None

    def test_match_different_path_returns_none(self):
        route = Route("GET", "/api/health", dummy_handler)
        assert route.match("GET", "/api/other") is None

    def test_match_extracts_single_param(self):
        route = Route("GET", "/api/items/{item_id}", dummy_handler)
        params = route.match("GET", "/api/items/42")
        assert params == {"item_id": "42"}

    def test_match_extracts_multiple_params(self):
        route = Route("GET", "/api/users/{user_id}/orders/{order_id}", dummy_handler)
        params = route.match("GET", "/api/users/7/orders/99")
        assert params == {"user_id": "7", "order_id": "99"}

    def test_match_param_with_special_chars_fails_on_slash(self):
        route = Route("GET", "/api/items/{item_id}", dummy_handler)
        assert route.match("GET", "/api/items/42/extra") is None

    def test_match_wildcard_method(self):
        route = Route("*", "/api/any", dummy_handler)
        assert route.match("GET", "/api/any") == {}
        assert route.match("POST", "/api/any") == {}
        assert route.match("DELETE", "/api/any") == {}

    def test_match_case_insensitive_method(self):
        route = Route("get", "/api/health", dummy_handler)
        assert route.match("GET", "/api/health") == {}
        assert route.match("get", "/api/health") == {}

    def test_match_empty_param_value(self):
        route = Route("GET", "/api/items/{item_id}", dummy_handler)
        # empty segment is not a valid capture because [^/]+ requires at least one char
        assert route.match("GET", "/api/items/") is None

    def test_handler_stored_correctly(self):
        route = Route("GET", "/api/health", dummy_handler)
        assert route.handler is dummy_handler


class TestRouter:
    def test_add_and_resolve_exact(self):
        router = Router()
        router.add("GET", "/api/health", dummy_handler)
        result = router.resolve("GET", "/api/health")
        assert result is not None
        handler, params = result
        assert handler is dummy_handler
        assert params == {}

    def test_get_shorthand(self):
        router = Router()
        router.get("/api/health", dummy_handler)
        result = router.resolve("GET", "/api/health")
        assert result is not None
        assert result[0] is dummy_handler

    def test_post_shorthand(self):
        router = Router()
        router.post("/api/data", dummy_handler)
        result = router.resolve("POST", "/api/data")
        assert result is not None
        assert result[0] is dummy_handler

    def test_resolve_returns_none_for_unknown(self):
        router = Router()
        router.get("/api/health", dummy_handler)
        assert router.resolve("GET", "/api/missing") is None

    def test_first_match_wins(self):
        router = Router()
        router.get("/api/items/{item_id}", dummy_handler)
        router.get("/api/items/special", another_handler)
        # first registered route should win
        result = router.resolve("GET", "/api/items/special")
        assert result is not None
        assert result[0] is dummy_handler
        assert result[1] == {"item_id": "special"}

    def test_resolve_with_params(self):
        router = Router()
        router.get("/api/users/{user_id}", dummy_handler)
        result = router.resolve("GET", "/api/users/42")
        assert result is not None
        assert result[1] == {"user_id": "42"}

    def test_multiple_routes(self):
        router = Router()
        router.get("/a", dummy_handler)
        router.get("/b", another_handler)
        assert router.resolve("GET", "/a")[0] is dummy_handler
        assert router.resolve("GET", "/b")[0] is another_handler

    def test_no_routes_returns_none(self):
        router = Router()
        assert router.resolve("GET", "/anything") is None
