"""Mocked followage, dormant links and optional search startup contracts."""
import ast
import calendar
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]

def definitions(path,names,ns):
    tree=ast.parse((ROOT/path).read_text(encoding="utf-8"));nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    future=ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[])),"utility-fixture","exec"),ns);return ns
class RequestError(Exception):pass
class TLSError(RequestError):pass
class Clock(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,10,8,12,tzinfo=timezone.utc)

def response(body,status=200):return SimpleNamespace(status_code=status,json=Mock(return_value=body))

class FollowageTests(unittest.TestCase):
    def setUp(self):
        self.user=response({"data":[{"id":"101"}]});self.follow=response({"data":[{"user_id":"101","followed_at":"2020-01-01T00:00:00Z"}]})
        self.validation=response({"user_id":"202","client_id":"synthetic-client","scopes":["moderator:read:followers"]})
        self.get=Mock(side_effect=lambda *a,**k:self.user if a[0].endswith('/users') else self.validation if a[0].endswith('/validate') else self.follow)
        self.bid=Mock(return_value="202");m=ModuleType("MedlarTV.core.stream_management");m.get_broadcaster_id=self.bid;m.CHANNEL_NAME="synthetic";self.headers=Mock(return_value={"Authorization":"Bearer synthetic","Client-ID":"synthetic-client"});m._broadcaster_headers=self.headers
        p=patch.dict(sys.modules,{m.__name__:m});p.start();self.addCleanup(p.stop)
        ns={"requests":SimpleNamespace(get=self.get,exceptions=SimpleNamespace(SSLError=TLSError,RequestException=RequestError)),"datetime":Clock,"timezone":timezone,"calendar":calendar,"re":re,"os":SimpleNamespace(getenv=lambda key,default=None:"#synthetic" if key=="TWITCH_CHANNEL" else default),"_twitch_headers_app":Mock(return_value={"Authorization":"Bearer synthetic"})}
        self.ns=definitions("MedlarTV/core/command_handlers.py",{"_calendar_anniversary","_fmt_calendar_followage","_followage_http_error","handle_followage"},ns)
    def call(self):return self.ns["handle_followage"]("synthetic","",{})
    def test_positive_follow_and_current_request_shape(self):
        self.assertIn("has been following synthetic",self.call());self.assertEqual(self.get.call_args.args[0],"https://api.twitch.tv/helix/channels/followers");self.assertEqual(self.get.call_args.kwargs["params"],{"user_id":"101","broadcaster_id":"202"})
    def test_empty_success_is_not_following_with_verified_user_authority(self):
        self.follow=response({"data":[],"total":8});self.assertIn("not following",self.call())
    def test_missing_user_requires_successful_empty_lookup(self):
        self.user=response({"data":[]});self.assertEqual(self.call(),"User not found: synthetic");self.assertEqual(self.get.call_count,1)
    def test_unauthorized_is_not_missing_user(self):
        self.user=response({"data":[]},401);self.assertIn("authorization",self.call());self.user.json.assert_not_called()
    def test_follower_scope_error_not_negative_follow(self):
        self.follow=response({"data":[]},403);self.assertIn("scope",self.call());self.follow.json.assert_not_called()
    def test_rate_limit_identified(self):
        self.follow=response({},429);self.assertIn("rate limited",self.call())
    def test_server_error_identified(self):
        self.follow=response({},503);self.assertIn("service error",self.call())
    def test_other_http_error_not_negative_follow(self):
        self.follow=response({},404);self.assertIn("HTTP request failed",self.call())
    def test_missing_data_is_malformed(self):
        self.follow=response({});self.assertIn("malformed",self.call())
    def test_invalid_user_shapes_are_not_missing(self):
        for body in ([],{"data":None},{"data":[{}]},{"data":[{"id":"101"},{"id":"999"}]}):
            self.user=response(body);self.assertIn("malformed",self.call())
    def test_wrong_follower_identity_is_malformed(self):
        self.follow=response({"data":[{"user_id":"999","followed_at":"2020-01-01T00:00:00Z"}]});self.assertIn("malformed",self.call())
    def test_bad_or_future_timestamp_not_negative_age(self):
        for stamp in (None,"bad","2020-01-01T00:00:00","2030-01-01T00:00:00Z"):
            self.follow=response({"data":[{"user_id":"101","followed_at":stamp}]});self.assertIn("malformed",self.call())
    def test_missing_broadcaster_stops_follower_request(self):
        self.bid.return_value=None;self.assertIn("broadcaster identity",self.call());self.assertEqual(self.get.call_count,1)
    def test_tls_and_transport_are_not_token_verdicts(self):
        for error,label in ((TLSError("synthetic"),"TLS"),(RequestError("synthetic"),"transport")):
            self.get.side_effect=error;out=self.call();self.assertIn(label,out);self.assertNotIn("invalid token",out)

class LinkParserTests(unittest.TestCase):
    def setUp(self):
        m=ModuleType("MedlarTV.core.content_filter");m.normalize_text=lambda s:s.lower();p=patch.dict(sys.modules,{m.__name__:m});p.start();self.addCleanup(p.stop)
        ns={"re":re,"urlsplit":urlsplit,"CONTENT_FILTER_PATH":SimpleNamespace(exists=lambda:False),"load_link_whitelist":lambda:{"trusted.example"},"os":SimpleNamespace(getenv=lambda k,d=None:d),"_has_excess_caps":lambda s:False,"_has_spam":lambda s:False}
        self.ns=definitions("MedlarTV/core/moderation.py",{"_extract_domains","check_message"},ns)
    def parse(self,value):return self.ns["_extract_domains"](value)
    def test_userinfo_is_not_hostname(self):self.assertEqual(self.parse("https://trusted.example@evil.example/path"),["evil.example"])
    def test_uppercase_scheme_and_host(self):self.assertEqual(self.parse("HTTPS://TRUSTED.EXAMPLE/path"),["trusted.example"])
    def test_port_does_not_change_hostname(self):self.assertEqual(self.parse("https://trusted.example:8443/path"),["trusted.example"])
    def test_ipv6_host_preserved(self):self.assertEqual(self.parse("http://[::1]"),["::1"])
    def test_prose_and_www_handled(self):self.assertEqual(self.parse("(www.trusted.example),"),["www.trusted.example"])
    def test_malformed_port_not_trusted(self):self.assertEqual(self.parse("https://trusted.example:bad/"),[""])
    def test_dormant_checker_rejects_userinfo_trick(self):
        out=self.ns["check_message"]("synthetic","https://trusted.example@evil.example",{"id":"synthetic"});self.assertFalse(out["is_allowed"]);self.assertEqual(out["action"],"delete")
    def test_dormant_checker_allows_exact_parsed_host(self):self.assertTrue(self.ns["check_message"]("synthetic","HTTPS://trusted.example:8080/path",{})["is_allowed"])

class SearchStartupTests(unittest.TestCase):
    def load(self,values=None):
        self.dotenv=Mock();self.web=Mock();self.web.text.return_value=[{"title":"synthetic"}];self.web.news.return_value=[];self.ddgs=Mock(return_value=self.web)
        self.page=SimpleNamespace(exists=lambda:True,title="synthetic",summary="Synthetic summary.",fullurl="https://example.invalid/");self.wikipedia=Mock(return_value=SimpleNamespace(page=Mock(return_value=self.page)))
        modules={}
        for name,attrs in {"dotenv":{"load_dotenv":self.dotenv},"ddgs":{"DDGS":self.ddgs},"wikipediaapi":{"Wikipedia":self.wikipedia}}.items():
            m=ModuleType(name);m.__dict__.update(attrs);modules[name]=m
        p=patch.dict(sys.modules,modules);p.start();self.addCleanup(p.stop)
        env=values or {};self.getenv=Mock(side_effect=lambda k,d=None:env.get(k,d));p=patch.object(os,"getenv",self.getenv);p.start();self.addCleanup(p.stop)
        ns={"__name__":"synthetic_search"};exec(compile((ROOT/"MedlarTV/core/web_search.py").read_text(encoding="utf-8"),"search-startup","exec"),ns);return ns
    def test_import_has_no_backend_or_dotenv_activity(self):
        self.load();self.dotenv.assert_not_called();self.ddgs.assert_not_called();self.wikipedia.assert_not_called();self.getenv.assert_not_called()
    def test_explicit_web_constructs_only_requested_backend(self):
        ns=self.load();self.assertTrue(ns["search_web"]("synthetic"));self.ddgs.assert_called_once();self.wikipedia.assert_not_called();self.dotenv.assert_called_once()
    def test_explicit_wiki_constructs_and_caches(self):
        ns=self.load();self.assertIsNotNone(ns["search_wikipedia"]("synthetic"));ns["search_wikipedia"]("synthetic");self.wikipedia.assert_called_once();self.ddgs.assert_not_called()
    def test_malformed_limit_does_not_break_import_or_request(self):
        ns=self.load({"WEB_SEARCH_MAX_RESULTS":"synthetic-invalid"});out=io.StringIO()
        with redirect_stdout(out):self.assertTrue(ns["search_web"]("synthetic"))
        self.assertEqual(self.web.text.call_args.kwargs["max_results"],3);self.assertNotIn("synthetic-invalid",out.getvalue())
    def test_zero_negative_or_invalid_explicit_limits_use_default(self):
        ns=self.load()
        for limit in (0,-1,"bad",True):ns["search_web"]("synthetic",limit);self.assertEqual(self.web.text.call_args.kwargs["max_results"],3)
    def test_valid_limit_is_not_capped(self):
        ns=self.load({"WEB_SEARCH_MAX_RESULTS":"7"});ns["search_web"]("synthetic");self.assertEqual(self.web.text.call_args.kwargs["max_results"],7)
    def test_disabled_backends_not_constructed(self):
        ns=self.load({"ENABLE_WEB_SEARCH":"false","ENABLE_WIKIPEDIA":"false"});self.assertEqual(ns["search_web"]("synthetic"),[]);self.assertIsNone(ns["search_wikipedia"]("synthetic"));self.ddgs.assert_not_called();self.wikipedia.assert_not_called()
    def test_backend_failure_is_local_to_explicit_request(self):
        ns=self.load();self.ddgs.side_effect=RuntimeError("synthetic");self.assertEqual(ns["search_web"]("synthetic"),[])
    def test_news_uses_lazy_factory(self):
        ns=self.load();ns["search_news"]("synthetic");self.ddgs.assert_called_once();self.wikipedia.assert_not_called()


class MemoryStatusEvidenceTests(unittest.TestCase):
    """Status must not mistake legacy defaults for scoped memory health."""
    def setUp(self):
        self.legacy=Mock(return_value={"moods":[],"last_reset":"synthetic"})
        self.profile=Mock(side_effect=AssertionError("viewer store read forbidden"))
        self.session=Mock(side_effect=AssertionError("session store read forbidden"))
        modules={}
        for name,attrs in {
            "MedlarTV.avatar.bridge.client":{"is_bridge_available":lambda:False},
            "MedlarTV.core.emotional_system":{"get_emotional_system":lambda:None},
            "MedlarTV.core.memory":{"load_memory":self.legacy},
            "MedlarTV.core.translation":{"get_supported_languages":lambda:[]},
            "MedlarTV.core.stream_management":{"get_access_token":lambda:None},
            "MedlarTV.core.viewer_profiles":{"ViewerProfileStore":self.profile},
            "MedlarTV.core.conversation_context":{"ConversationContextStore":self.session},
        }.items():
            m=ModuleType(name);m.__dict__.update(attrs);modules[name]=m
        p=patch.dict(sys.modules,modules);p.start();self.addCleanup(p.stop)
        self.ns=definitions("MedlarTV/core/command_handlers.py",{"handle_status"},{})
    def status(self,context=None):return self.ns["handle_status"]("synthetic","",context or {})
    def assert_unverified(self,out):
        for label in ("LegacyMoodMemory:UNVERIFIED","ViewerMemory:UNVERIFIED","SessionMemory:UNVERIFIED"):self.assertIn(label,out)
        self.assertNotIn("Memory:OK",out);self.assertNotIn("Memory:FAILED",out)
        self.legacy.assert_not_called();self.profile.assert_not_called();self.session.assert_not_called()
    def test_nonempty_legacy_default_is_not_health(self):self.assert_unverified(self.status())
    def test_missing_legacy_store_stays_unverified_without_read(self):
        self.legacy.side_effect=FileNotFoundError("synthetic-private-path");self.assert_unverified(self.status())
    def test_unreadable_legacy_store_stays_unverified_without_read(self):
        self.legacy.side_effect=PermissionError("synthetic-private-path");self.assert_unverified(self.status())
    def test_malformed_or_empty_defaults_do_not_become_health(self):
        for data in (None,{},[],{"moods":["synthetic"]}):self.legacy.return_value=data;self.assert_unverified(self.status())
    def test_legacy_import_unavailable_does_not_conflate_store_failure(self):
        with patch.dict(sys.modules,{"MedlarTV.core.memory":None}):self.assert_unverified(self.status())
    def test_viewer_session_stores_and_context_bodies_are_not_inspected(self):
        marker="synthetic-private-body";out=self.status({"viewer_memory":marker,"session_memory":marker});self.assert_unverified(out);self.assertNotIn(marker,out)
    def test_other_status_evidence_and_reply_shape_are_preserved(self):
        out=self.status({"socket_connected":True,"bot_nick":"SyntheticBot"});self.assert_unverified(out)
        for label in ("IRC:OK","Bridge:FAILED","Emotions:FAILED","Moderation:NOT_INTEGRATED","Translation:FAILED","StreamAPI:FAILED"):self.assertIn(label,out)
        self.assertIn("System Status:",out);self.assertTrue(out.endswith("Connected as SyntheticBot"))


class ApprovedFollowageTests(unittest.TestCase):
    setUp = FollowageTests.setUp
    call = FollowageTests.call
    # Inherit setup only; inherited contract tests run independently too.
    def test_no_argument_targets_invoker(self):
        self.call();self.assertEqual(self.get.call_args_list[0].kwargs["params"],{"login":"synthetic"})
    def test_explicit_target_normalized(self):
        self.ns["handle_followage"]("synthetic"," @Other_User ",{});self.assertEqual(self.get.call_args_list[0].kwargs["params"],{"login":"other_user"})
    def test_invalid_target_does_not_read_provider_or_request(self):
        for value in ("@","@@name","two users","a/b","x"*26,"é"):
            self.assertIn("valid Twitch username",self.ns["handle_followage"]("synthetic",value,{}))
        self.headers.assert_not_called();self.get.assert_not_called()
    def test_app_provider_never_called(self):
        self.call();self.ns["_twitch_headers_app"].assert_not_called();self.headers.assert_called_once()
    def test_missing_user_provider_stops_requests(self):
        self.headers.return_value={};self.assertIn("authorization",self.call());self.get.assert_not_called()
    def test_unverified_empty_details_never_not_following(self):
        for value in ({"user_id":"999","client_id":"synthetic-client","scopes":["moderator:read:followers"]},{"user_id":"202","client_id":"wrong","scopes":["moderator:read:followers"]},{"user_id":"202","client_id":"synthetic-client","scopes":[]}):
            self.validation=response(value);self.assertIn("requirements",self.call());self.assertFalse(any(c.args[0].endswith('/followers') for c in self.get.call_args_list));self.get.reset_mock()
    def test_validation_error_not_negative_follow(self):
        self.validation=response({},401);self.assertIn("authorization",self.call());self.validation.json.assert_not_called()
    def test_malformed_authority_not_negative_follow(self):
        self.validation=response([]);self.assertIn("malformed",self.call())
    def test_configured_channel_not_hardcoded(self):
        sys.modules["MedlarTV.core.stream_management"].CHANNEL_NAME="another_configured_channel";self.assertIn("another_configured_channel",self.call())
    def test_calendar_leap_and_month_end(self):
        cases=(("2020-02-29T12:00:00+00:00","2021-02-28T12:00:00+00:00","1 year"),("2024-01-31T12:00:00+00:00","2024-02-29T12:00:00+00:00","1 month"),("2024-01-31T12:00:00+00:00","2024-03-31T12:00:00+00:00","2 months"),("2024-01-31T12:00:00+00:00","2024-02-29T11:59:59+00:00","28 days 23 hours 59 minutes 59 seconds"))
        for start,end,want in cases:self.assertEqual(self.ns["_fmt_calendar_followage"](datetime.fromisoformat(start),datetime.fromisoformat(end)),want)
    def test_timezone_and_seconds(self):
        self.follow=response({"data":[{"user_id":"101","followed_at":"2026-10-08T13:58:59+02:00"}]});self.assertTrue(self.call().endswith("1 minute 1 second"))
    def test_calendar_full_interval_and_zero(self):
        fmt=self.ns["_fmt_calendar_followage"];self.assertEqual(fmt(datetime(2020,1,1,tzinfo=timezone.utc),Clock.now()),"6 years 9 months 7 days 12 hours");self.assertEqual(fmt(Clock.now(),Clock.now()),"0 seconds")
    def test_naive_and_future_calendar_rejected(self):
        for start in (datetime(2020,1,1),datetime(2030,1,1,tzinfo=timezone.utc)):
            with self.assertRaises(ValueError):self.ns["_fmt_calendar_followage"](start,Clock.now())
