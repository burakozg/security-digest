"""Tests for weekly delivery: gather daily, consolidate, send once a week.

The failure this feature has to avoid is silent and expensive, so most of what
is checked here is about what happens when something goes wrong -- a send day
missed, a mail server refusing, a consolidation call failing. A week of news
disappearing produces no error anywhere; it just means an inbox stays empty.
"""

import datetime
import json

import pytest

import src.weekly as weekly
from src.weekly import (
    DEFAULT_SEND_WEEKDAY, clear, is_due, is_weekly, last_sent, mark_sent,
    oldest_queued, pending, queue, send_day_label, send_due, send_weekday,
)

UTC = datetime.timezone.utc

# 2026-08-22 is a Saturday.
SATURDAY = datetime.datetime(2026, 8, 22, 6, 30, tzinfo=UTC)
TUESDAY = datetime.datetime(2026, 8, 18, 6, 30, tzinfo=UTC)

WEEKLY_DIGEST = {
    "title": "Wife's News",
    "sections": ["key", "notable", "mention"],
    "weekly_sections": ["key", "notable"],
    "frequency": "weekly",
    "send_day": "sat",
    "to": "wife@example.com",
}


def _config(tz="UTC"):
    return {
        "schedule": {"timezone": tz},
        "llm": {"categories": ["key", "notable", "mention", "exclude"],
                "fallback_category": "mention"},
    }


def queue_at(when, items, title, db):
    """Queue items as if the run that produced them happened at `when`."""
    import unittest.mock
    with unittest.mock.patch.object(weekly, "_utcnow", lambda: when):
        queue(items, title, db)


def _item(title, category="key", source="Vattenfall"):
    return {"title": title, "link": f"https://example.com/{title}", "source": source,
            "summary": f"{title} happened.", "category": category}


# --- queue ------------------------------------------------------------------


def test_queue_pending_and_clear_round_trip(tmp_path):
    db = tmp_path / "digest.db"
    queue([_item("A"), _item("B")], "Wife's News", db)

    ids, items = pending("wifes-news", db)
    assert [i["title"] for i in items] == ["A", "B"]
    assert len(ids) == 2

    clear(ids, db)
    assert pending("wifes-news", db) == ([], [])


def test_each_digest_has_its_own_queue(tmp_path):
    db = tmp_path / "digest.db"
    queue([_item("Hers")], "Wife's News", db)
    queue([_item("His")], "His News", db)

    assert [i["title"] for i in pending("wifes-news", db)[1]] == ["Hers"]
    assert [i["title"] for i in pending("his-news", db)[1]] == ["His"]


def test_queued_items_keep_the_links_clustering_built(tmp_path):
    """The whole reason for a queue table rather than reusing `history`: the
    per-outlet link list survives, and the weekly pass needs it."""
    db = tmp_path / "digest.db"
    item = {**_item("Merged"), "links": [{"publisher": "DN", "link": "https://dn.se/1"},
                                         {"publisher": "SvD", "link": "https://svd.se/1"}]}
    queue([item], "Wife's News", db)
    assert pending("wifes-news", db)[1][0]["links"] == item["links"]


def test_an_unreadable_queued_row_is_reported_for_deletion(tmp_path, caplog):
    """It must come back in `ids` even though it is not in `items`, or a corrupt
    row would be skipped on every send and never cleaned up."""
    db = tmp_path / "digest.db"
    queue([_item("Good")], "Wife's News", db)
    conn = weekly.get_connection(db)
    conn.execute("INSERT INTO weekly_pending (digest_slug, queued_at, item) VALUES (?, ?, ?)",
                 ("wifes-news", weekly._utcnow().isoformat(), "{not json"))
    conn.commit()
    conn.close()

    ids, items = pending("wifes-news", db)
    assert len(ids) == 2 and [i["title"] for i in items] == ["Good"]


def test_queueing_nothing_writes_nothing(tmp_path):
    db = tmp_path / "digest.db"
    queue([], "Wife's News", db)
    assert oldest_queued("wifes-news", db) is None


# --- digest fields ----------------------------------------------------------


def test_frequency_defaults_to_daily():
    assert is_weekly({}) is False
    assert is_weekly({"frequency": "daily"}) is False
    assert is_weekly({"frequency": "Weekly"}) is True


def test_send_day_accepts_short_and_long_names():
    assert send_weekday({"send_day": "mon"}) == 0
    assert send_weekday({"send_day": "Sunday"}) == 6
    assert send_day_label({"send_day": "wed"}) == "Wednesday"


def test_an_unknown_send_day_falls_back_rather_than_never_sending(caplog):
    with caplog.at_level("WARNING"):
        assert send_weekday({"send_day": "someday", "title": "T"}) == DEFAULT_SEND_WEEKDAY
    assert "Unknown send_day" in caplog.text


# --- due check --------------------------------------------------------------


def test_due_on_the_readers_day(tmp_path):
    db = tmp_path / "digest.db"
    queue_at(TUESDAY, [_item("A")], "Wife's News", db)
    assert is_due(WEEKLY_DIGEST, _config(), SATURDAY, db) is True


def test_not_due_on_any_other_day(tmp_path):
    db = tmp_path / "digest.db"
    queue_at(TUESDAY, [_item("A")], "Wife's News", db)
    assert is_due(WEEKLY_DIGEST, _config(), TUESDAY, db) is False


def test_not_due_again_once_this_weeks_send_has_happened(tmp_path):
    """A manual /run later the same Saturday must not mail the week twice."""
    db = tmp_path / "digest.db"
    mark_sent("wifes-news", SATURDAY, db)
    queue_at(SATURDAY + datetime.timedelta(hours=1), [_item("A")], "Wife's News", db)
    assert is_due(WEEKLY_DIGEST, _config(), SATURDAY + datetime.timedelta(hours=3), db) is False
    # ... nor on any day before the next one comes round.
    assert is_due(WEEKLY_DIGEST, _config(), SATURDAY + datetime.timedelta(days=3), db) is False


def test_a_missed_send_day_is_caught_up_rather_than_costing_the_week(tmp_path):
    """The container being down on Saturday must not mean no digest until the
    next one: Saturday has passed with no send, so the send is still owed."""
    db = tmp_path / "digest.db"
    mark_sent("wifes-news", SATURDAY - datetime.timedelta(days=7), db)
    queue_at(SATURDAY - datetime.timedelta(days=2), [_item("A")], "Wife's News", db)
    assert is_due(WEEKLY_DIGEST, _config(), SATURDAY + datetime.timedelta(days=2), db) is True


def test_a_failed_send_is_retried_on_the_very_next_run(tmp_path):
    """The rule this replaced measured seven days from the last send, so a mail
    server refusing on Saturday cost the reader the whole week."""
    db = tmp_path / "digest.db"
    mark_sent("wifes-news", SATURDAY - datetime.timedelta(days=7), db)
    queue_at(SATURDAY - datetime.timedelta(days=1), [_item("A")], "Wife's News", db)
    # Saturday's send raised, so nothing was stamped. Sunday's run picks it up.
    assert is_due(WEEKLY_DIGEST, _config(), SATURDAY + datetime.timedelta(days=1), db) is True


def test_a_new_reader_waits_for_their_first_send_day(tmp_path):
    """With no send to measure from, the queue's own age stands in -- they are
    not owed the Saturday that passed before they existed."""
    db = tmp_path / "digest.db"
    queue_at(SATURDAY + datetime.timedelta(days=2), [_item("A")], "Wife's News", db)
    assert is_due(WEEKLY_DIGEST, _config(), SATURDAY + datetime.timedelta(days=4), db) is False
    assert is_due(WEEKLY_DIGEST, _config(), SATURDAY + datetime.timedelta(days=7), db) is True


def test_nothing_queued_and_never_sent_is_not_due(tmp_path):
    assert is_due(WEEKLY_DIGEST, _config(), SATURDAY, tmp_path / "digest.db") is False


def test_the_send_day_is_measured_in_the_schedules_timezone(tmp_path):
    """The containers set no TZ and run UTC while schedule.txt says
    Europe/Stockholm. This instant is Friday evening in UTC and Saturday morning
    in Auckland, so the two answers differ and the schedule's must win."""
    db = tmp_path / "digest.db"
    friday_utc = datetime.datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
    queue_at(friday_utc - datetime.timedelta(days=3), [_item("A")], "Wife's News", db)
    assert is_due(WEEKLY_DIGEST, _config("UTC"), friday_utc, db) is False
    assert is_due(WEEKLY_DIGEST, _config("Pacific/Auckland"), friday_utc, db) is True


def test_an_unknown_timezone_falls_back_to_utc_rather_than_raising(tmp_path, caplog):
    db = tmp_path / "digest.db"
    queue_at(TUESDAY, [_item("A")], "Wife's News", db)
    with caplog.at_level("WARNING"):
        assert is_due(WEEKLY_DIGEST, _config("Mars/Olympus"), SATURDAY, db) is True
    assert "Unknown schedule timezone" in caplog.text


# --- sending ----------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    """send_one with the LLM and the mail server replaced."""
    state = {"delivered": [], "recorded": [], "intro": "It was a week."}
    monkeypatch.setattr(weekly, "consolidate", lambda items, config: list(items))
    monkeypatch.setattr(weekly, "write_intro", lambda entries, config: state["intro"])
    monkeypatch.setattr(weekly, "build_digest",
                        lambda entries, config, d, intro=None, date_label=None:
                            f"{date_label}|{intro}|{len(entries)}")
    monkeypatch.setattr(
        weekly, "deliver",
        lambda content, config, title=None, digest_cfg=None, subject_note=None:
            state["delivered"].append({"title": title, "content": content,
                                       "note": subject_note, "to": (digest_cfg or {}).get("to")}))
    monkeypatch.setattr(weekly, "record_sent",
                        lambda entries, title, config, db_path=None:
                            state["recorded"].extend(e["title"] for e in entries))
    return state


def test_a_week_with_nothing_in_it_produces_no_email(sent, tmp_path):
    """An email with nothing in it is worse than no email. The week is still
    stamped as considered -- without that, every run for the rest of the week
    would find a send still owed and fire one off the moment an item arrived."""
    db = tmp_path / "digest.db"
    mark_sent("wifes-news", SATURDAY - datetime.timedelta(days=7), db)

    assert send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db) == 0
    assert sent["delivered"] == []
    assert last_sent("wifes-news", db) == SATURDAY


def test_a_successful_send_clears_the_queue_and_stamps_the_week(sent, tmp_path):
    db = tmp_path / "digest.db"
    queue_at(TUESDAY, [_item("A"), _item("B", "notable")], "Wife's News", db)

    assert send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db) == 1

    assert len(sent["delivered"]) == 1
    assert sent["delivered"][0]["to"] == "wife@example.com"
    assert sent["recorded"] == ["A", "B"]
    assert pending("wifes-news", db) == ([], [])
    assert last_sent("wifes-news", db) == SATURDAY


def test_history_records_the_consolidated_entries_not_the_daily_ones(sent, tmp_path, monkeypatch):
    """record_sent means "this was emailed", so what it logs has to be what
    landed in the inbox -- one merged entry, not the three days it came from."""
    db = tmp_path / "digest.db"
    monkeypatch.setattr(weekly, "consolidate",
                        lambda items, config: [_item("Merged story")])
    queue_at(TUESDAY, [_item("Mon"), _item("Tue"), _item("Thu")], "Wife's News", db)

    send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db)
    assert sent["recorded"] == ["Merged story"]


def test_a_refused_send_keeps_the_whole_week_queued(sent, tmp_path, monkeypatch, caplog):
    """The regression that matters most: a mail server refusing must cost a
    retry, not the week."""
    db = tmp_path / "digest.db"

    def boom(*a, **k):
        raise ValueError("SMTP authentication failed")

    monkeypatch.setattr(weekly, "deliver", boom)
    queue_at(TUESDAY, [_item("A"), _item("B")], "Wife's News", db)

    with caplog.at_level("ERROR"):
        assert send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db) == 0

    assert [i["title"] for i in pending("wifes-news", db)[1]] == ["A", "B"]
    assert last_sent("wifes-news", db) is None
    assert sent["recorded"] == []


def test_one_readers_failure_does_not_stop_another(sent, tmp_path, monkeypatch):
    db = tmp_path / "digest.db"
    other = {**WEEKLY_DIGEST, "title": "His News", "to": "his@example.com"}
    queue_at(TUESDAY, [_item("Hers")], "Wife's News", db)
    queue_at(TUESDAY, [_item("His")], "His News", db)

    real = weekly.build_digest
    monkeypatch.setattr(weekly, "build_digest",
                        lambda entries, config, d, **k:
                            (_ for _ in ()).throw(RuntimeError("bad prompt"))
                            if d["title"] == "Wife's News" else real(entries, config, d, **k))

    assert send_due(_config(), [WEEKLY_DIGEST, other], SATURDAY, db) == 1
    assert [d["title"] for d in sent["delivered"]] == ["His News"]
    assert pending("wifes-news", db)[1] != []


def test_the_subject_note_names_the_period_covered(sent, tmp_path):
    db = tmp_path / "digest.db"
    mark_sent("wifes-news", SATURDAY - datetime.timedelta(days=7), db)
    queue_at(TUESDAY, [_item("A")], "Wife's News", db)

    send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db)
    assert sent["delivered"][0]["note"] == "week of 15 Aug – 22 Aug"
    # And the same label heads the digest itself, in place of today's date.
    assert sent["delivered"][0]["content"].startswith("week of 15 Aug – 22 Aug|")


def test_the_intro_is_rendered_into_the_edition(sent, tmp_path):
    db = tmp_path / "digest.db"
    queue_at(TUESDAY, [_item("A")], "Wife's News", db)
    send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db)
    assert "|It was a week.|" in sent["delivered"][0]["content"]


# --- section narrowing ------------------------------------------------------


def test_the_weekly_edition_leaves_out_the_sections_it_does_not_carry(sent, tmp_path):
    db = tmp_path / "digest.db"
    queue_at(TUESDAY, [_item("Big"), _item("Small", "mention")], "Wife's News", db)

    send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db)
    assert sent["recorded"] == ["Big"]


def test_an_item_the_week_promoted_is_kept(sent, tmp_path, monkeypatch):
    """Narrowing happens after consolidation, which is what lets a Monday
    `mention` that mattered by Friday reach the email at all."""
    db = tmp_path / "digest.db"
    monkeypatch.setattr(weekly, "consolidate",
                        lambda items, config: [{**i, "category": "notable"} for i in items])
    queue_at(TUESDAY, [_item("Looked minor", "mention")], "Wife's News", db)

    send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db)
    assert sent["recorded"] == ["Looked minor"]


def test_a_week_that_produced_nothing_worth_sending_still_clears_the_queue(sent, tmp_path):
    """Those items HAVE been considered. Holding them would mail them next week
    as though they were new."""
    db = tmp_path / "digest.db"
    queue_at(TUESDAY, [_item("Trivia", "mention")], "Wife's News", db)

    assert send_due(_config(), [WEEKLY_DIGEST], SATURDAY, db) == 0
    assert sent["delivered"] == []
    assert pending("wifes-news", db) == ([], [])
    assert last_sent("wifes-news", db) == SATURDAY


def test_a_digest_with_no_weekly_sections_carries_its_full_list(sent, tmp_path):
    db = tmp_path / "digest.db"
    digest = {k: v for k, v in WEEKLY_DIGEST.items() if k != "weekly_sections"}
    queue_at(TUESDAY, [_item("Big"), _item("Small", "mention")], "Wife's News", db)

    send_due(_config(), [digest], SATURDAY, db)
    assert sent["recorded"] == ["Big", "Small"]


def test_daily_digests_are_left_alone(sent, tmp_path):
    db = tmp_path / "digest.db"
    daily = {**WEEKLY_DIGEST, "frequency": "daily"}
    queue_at(TUESDAY, [_item("A")], "Wife's News", db)
    assert send_due(_config(), [daily], SATURDAY, db) == 0
    assert sent["delivered"] == []


# --- consolidation ----------------------------------------------------------


@pytest.fixture
def llm(monkeypatch, tmp_path):
    """The clustering call, stubbed, with real prompt templates on disk."""
    import src.summariser as summ

    (tmp_path / "weekly.txt").write_text("WEEK\n{items}\n")
    (tmp_path / "weekly_intro.txt").write_text("INTRO\n{entries}\n")
    monkeypatch.setattr(weekly, "WEEKLY_PROMPT_PATH", tmp_path / "weekly.txt")
    monkeypatch.setattr(weekly, "WEEKLY_INTRO_PROMPT_PATH", tmp_path / "weekly_intro.txt")

    state = {"responses": [], "prompts": []}
    monkeypatch.setattr(summ, "_get_client", lambda config: object())

    def fake(client, config, prompt, schema, kind="summarise"):
        state["prompts"].append(prompt)
        response = state["responses"].pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)

    monkeypatch.setattr(summ, "_call_llm", fake)
    return state


def test_consolidation_merges_a_story_with_its_follow_up(llm):
    llm["responses"] = [{"clusters": [
        {"title": "Deal closed", "summary": "Signed Thursday.",
         "category": "key", "members": [0, 1]},
    ]}]
    entries = weekly.consolidate(
        [_item("Deal rumoured"), _item("Deal signed")], _config())

    assert [e["title"] for e in entries] == ["Deal closed"]
    assert [l["link"] for l in entries[0]["links"]] == [
        "https://example.com/Deal rumoured", "https://example.com/Deal signed"]


def test_consolidation_calls_once_per_topic(llm):
    llm["responses"] = [
        {"clusters": [{"title": "V", "summary": "s", "category": "key", "members": [0]}]},
        {"clusters": [{"title": "N", "summary": "s", "category": "key", "members": [0]}]},
    ]
    entries = weekly.consolidate(
        [_item("A", source="Vattenfall"), _item("B", source="Nvidia")], _config())

    assert len(llm["prompts"]) == 2
    assert sorted(e["title"] for e in entries) == ["N", "V"]


def test_the_pass_sees_the_summary_written_on_the_day(llm):
    """A queued item's prose lives in `summary`; the cluster formatter reads
    `description`. Without the mapping the model is handed empty items."""
    llm["responses"] = [{"clusters": [
        {"title": "T", "summary": "s", "category": "key", "members": [0]}]}]
    weekly.consolidate([_item("Deal signed")], _config())
    assert "Deal signed happened." in llm["prompts"][0]


def test_a_failed_topic_keeps_its_items_as_they_were_summarised(llm, caplog):
    """They are already summarised, so falling through costs the reader some
    duplication. Re-summarising a week of items would cost real money."""
    llm["responses"] = [RuntimeError("model unavailable")]
    with caplog.at_level("WARNING"):
        entries = weekly.consolidate([_item("A"), _item("B")], _config())

    assert [e["title"] for e in entries] == ["A", "B"]
    assert "keeping its items as they were summarised" in caplog.text


def test_consolidating_nothing_makes_no_call(llm):
    assert weekly.consolidate([], _config()) == []
    assert llm["prompts"] == []


def test_an_absurd_queue_is_capped_rather_than_sent_as_one_prompt(llm, caplog, monkeypatch):
    monkeypatch.setattr(weekly, "MAX_CONSOLIDATED_ITEMS", 3)
    llm["responses"] = [{"clusters": [
        {"title": "T", "summary": "s", "category": "key", "members": [0, 1, 2]}]}]
    with caplog.at_level("WARNING"):
        weekly.consolidate([_item(f"i{n}") for n in range(10)], _config())
    assert "Sends have probably been failing" in caplog.text
    assert "i9" in llm["prompts"][0] and "i0" not in llm["prompts"][0]


def test_the_intro_is_optional_and_never_blocks_the_send(llm, caplog):
    llm["responses"] = [RuntimeError("model unavailable")]
    with caplog.at_level("WARNING"):
        assert weekly.write_intro([_item("A")], _config()) == ""
    assert "sending the digest without one" in caplog.text


def test_the_intro_comes_back_unwrapped(llm):
    llm["responses"] = [{"intro": "  A quiet week.  "}]
    assert weekly.write_intro([_item("A")], _config()) == "A quiet week."
