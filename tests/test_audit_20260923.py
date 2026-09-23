"""Regression coverage for the September audit's data and access boundaries."""
import unittest
from unittest.mock import patch

import harness
harness.setup(RADARR_URL="http://radarr.invalid", RADARR_API_KEY="test",
              RADARR_QUALITY_PROFILE_ID="1", RADARR_ROOT_FOLDER="/films",
              SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="test",
              SONARR_QUALITY_PROFILE_ID="1", SONARR_ROOT_FOLDER="/series")

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import api, arr, buskarr, compat_nextread, imports, jellyfin, media, radarr, sonarr, store, wants
from app.books import audible, series, shelves

USER = jellyfin.User("audit-user", "Listener")
MEDIUM = media.Medium(media.MOVIE, "Films", ("movie",), 3, ("films",))
store.init()


def transport_client(handler):
    return httpx.Client(base_url="http://test.invalid", transport=httpx.MockTransport(handler))


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        with store.db() as conn:
            conn.execute("DELETE FROM requests")

    def test_punctuation_is_preserved_in_plain_title_lists(self):
        for text in ("Crouching Tiger, Hidden Dragon\nDune", "Title\nCrouching Tiger, Hidden Dragon\nDune"):
            rows, _, _ = imports.rows(imports.read(text), media.MOVIE, "movie")
            self.assertEqual([r.title for r in rows], ["Crouching Tiger, Hidden Dragon", "Dune"])

    def test_an_owned_near_match_cannot_be_selected_for_acquisition(self):
        hit = {"itemKey": "B001", "title": "Dune", "artist": "Frank Herbert", "owned": True}
        with patch.object(wants, "search", return_value=[hit]):
            row = imports.match(USER, media.BOOK, "book", imports.Row(1, "Dune"), set())
        self.assertEqual(row["state"], imports.HELD)
        self.assertTrue(row["hit"]["owned"])

    def test_owned_provider_id_is_not_added_or_charged(self):
        with patch.object(media, "get", return_value=MEDIUM), patch.object(media, "owned", return_value=jellyfin.Owned(movie_tmdb=frozenset({"12"}))), patch.object(radarr, "add") as add:
            self.assertEqual(wants.want(USER, media.MOVIE, "tmdb:12")[0], wants.IN_LIBRARY)
        add.assert_not_called()
        self.assertEqual(store.spent_today(USER.key, media.MOVIE, 0), 0)

    def test_preexisting_arr_entries_never_become_owned_backend_ids(self):
        for module, pid in ((radarr, "tmdbId"), (sonarr, "tvdbId")):
            tool = module.backend()
            with patch.object(arr.Arr, "existing", return_value={"id": 81, pid: 12, "title": "Owned elsewhere"}):
                result = module.add("12")
            self.assertTrue(result.ok)
            self.assertFalse(result.created)
            self.assertEqual(result.backend_id, "")

    def test_cancelling_an_existing_household_entry_never_deletes_it(self):
        with patch.object(media, "get", return_value=MEDIUM), patch.object(media, "owned", return_value=jellyfin.Owned()), patch.object(radarr, "add", return_value=arr.AddResult(True, "Already in Radarr.", created=False)), patch.object(radarr, "cancel") as cancel:
            wants.want(USER, media.MOVIE, "tmdb:12")
            self.assertEqual(store.spent_today(USER.key, media.MOVIE, 0), 0)
            self.assertTrue(wants.cancel(USER, media.MOVIE, "tmdb:12")[0])
        cancel.assert_not_called()

    def test_a_shared_entry_retains_the_original_creator_for_the_last_canceller(self):
        other = jellyfin.User("other", "Other")
        with patch.object(media, "get", return_value=MEDIUM), patch.object(media, "owned", return_value=jellyfin.Owned()), patch.object(radarr, "add", side_effect=[arr.AddResult(True, "Sent to Radarr.", "81"), arr.AddResult(True, "Already in Radarr.", created=False)]), patch.object(radarr, "cancel", return_value=True) as cancel:
            wants.want(USER, media.MOVIE, "tmdb:12")
            wants.want(other, media.MOVIE, "tmdb:12")
            wants.cancel(USER, media.MOVIE, "tmdb:12")
            cancel.assert_not_called()
            wants.cancel(other, media.MOVIE, "tmdb:12")
        cancel.assert_called_once_with("81")

    def test_arr_cancellation_preserves_entries_with_downloaded_files(self):
        for module, row in ((radarr, {"id": 81, "hasFile": True}), (sonarr, {"id": 81, "statistics": {"episodeFileCount": 1}})):
            calls = []
            def answer(request):
                calls.append(request.method)
                return httpx.Response(200, json=row)
            with patch.object(arr.Arr, "client", side_effect=lambda *a, **k: transport_client(answer)):
                self.assertTrue(module.cancel("81"))
            self.assertEqual(calls, ["GET"])

    def test_arr_cancellation_refuses_to_delete_when_current_files_are_unknown(self):
        calls = []
        def answer(request):
            calls.append(request.method)
            return httpx.Response(503)
        with patch.object(arr.Arr, "client", side_effect=lambda *a, **k: transport_client(answer)):
            self.assertFalse(radarr.cancel("81"))
        self.assertEqual(calls, ["GET"])

    def test_arr_outage_is_an_api_error_instead_of_no_results(self):
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api.caller] = lambda: USER
        with patch.object(media, "get", return_value=MEDIUM), patch.object(media, "owned", return_value=jellyfin.Owned()), patch.object(arr.Arr, "client", side_effect=lambda *a, **k: transport_client(lambda r: httpx.Response(503))):
            response = TestClient(app).get("/api/v1/search?medium=movie&q=Dune")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Radarr", response.json()["detail"])

    def test_a_member_cannot_clear_household_acquisitions(self):
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api.caller] = lambda: USER
        with patch.object(api.gone, "clear") as clear:
            response = TestClient(app).post("/api/v1/deleted", json={"type": "Series", "providerIds": {"Tvdb": "12"}})
        self.assertEqual(response.status_code, 403)
        clear.assert_not_called()

    def test_account_lookup_translates_service_key_and_server_failures(self):
        for status in (401, 403, 503):
            with patch.object(jellyfin, "_client", side_effect=lambda: transport_client(lambda r: httpx.Response(status))):
                for call in (jellyfin.all_users, jellyfin.accounts, lambda: jellyfin.user("name"), lambda: jellyfin.account("id")):
                    with self.assertRaises(jellyfin.JellyfinUnavailable):
                        call()

    def test_token_introspection_does_not_reject_credentials_during_an_outage(self):
        client = transport_client(lambda r: httpx.Response(503))
        with patch.object(jellyfin.httpx, "Client", return_value=client):
            with self.assertRaises(jellyfin.JellyfinUnavailable):
                jellyfin.user_from_token("throwaway")

    def test_blank_series_does_not_read_or_adopt_seriesless_books(self):
        with patch.object(jellyfin, "books") as books:
            with self.assertRaises(series.NotASeries):
                series.plan(USER, "  ")
        books.assert_not_called()

    def test_failed_similarity_requests_are_not_cached_as_empty(self):
        client_factory = httpx.Client
        def new_client(**kwargs):
            return client_factory(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        with patch.object(audible, "_base", return_value="http://audible.invalid"), patch.object(audible.store, "get_sims", return_value=None), patch.object(audible.store, "put_sims") as put, patch.object(audible.httpx, "Client", side_effect=new_client):
            self.assertEqual(audible.sims("B001"), [])
        put.assert_not_called()

    def test_distinct_recordings_have_distinct_request_identity(self):
        short = buskarr._result({"artist": "A", "title": "B", "duration": 120}, "track")
        long = buskarr._result({"artist": "A", "title": "B", "duration": 300}, "track")
        self.assertNotEqual(short["itemKey"], long["itemKey"])

    def test_compat_dismiss_and_restore_keep_the_stored_shelf(self):
        with patch.object(compat_nextread.wants, "dismiss"), patch.object(compat_nextread.wants, "restore", return_value=True), patch.object(shelves, "invalidate") as invalidate, patch.object(shelves, "forget_asin") as forget, patch.object(shelves, "expire") as expire:
            compat_nextread.post_dismiss(USER, "B001", None)
            compat_nextread.post_restore(USER, "B001", None)
        invalidate.assert_not_called()
        forget.assert_called_once_with("B001", user_key=USER.key)
        self.assertEqual(expire.call_count, 2)


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False)
    finally:
        harness.cleanup()
    raise SystemExit(not result.result.wasSuccessful())
