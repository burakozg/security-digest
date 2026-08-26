"""Note rendering, the topic threshold, and the escaping that keeps untrusted
feed text from writing links of its own."""

import pytest

from src.vault import write_notes
from src.vault.render import relink, story_note, story_note_name, topic_links
from src.vault.text import wikilink


def item(**kwargs):
    base = {
        "title": "Critical RCE in FortiOS", "link": "https://a/1", "source": "BleepingComputer",
        "publisher": "BleepingComputer", "category": "news", "domain": "security",
        "published": "2026-08-24T09:00:00", "summary": "Fortinet patched it.",
        "description": "Some feed text.", "entities": ["Fortinet", "CVE-2026-1234"],
    }
    return {**base, **kwargs}


DIGEST = {"title": "Security Digest", "sections": ["news", "thought_leadership"],
          "labels": {"news": "News & updates"}}
CONFIG = {"vault": {"enabled": True, "min_mentions": 2}}


# --- escaping ---------------------------------------------------------------

def test_a_feed_title_cannot_close_our_wikilink():
    """Backslash-escaping is not a defence inside `[[...]]`: Obsidian does not
    process escapes there and closes the link at the first `]]`, so anything
    after it would land outside as attacker-chosen Markdown."""
    hostile = "Evil ]] [[malware|steal]] rest"
    link = wikilink("note-a", hostile)

    assert link.count("[[") == 1
    assert link.count("]]") == 1
    assert link.startswith("[[note-a|")
    assert "malware" in link  # kept as text, harmless



def test_an_unresolved_topic_is_plain_text_not_a_dangling_link():
    line = topic_links(["Fortinet", "Okta"], {"Fortinet": "fortinet"})
    assert "[[fortinet|Fortinet]]" in line
    assert "[[okta" not in line.lower()
    assert "Okta" in line


def test_javascript_urls_never_reach_a_markdown_link():
    note = story_note(item(link="javascript:alert(1)"), "Security Digest", "2026-08-24")
    assert "javascript:" not in note.split("---", 2)[2]  # not in the body
    assert "](" not in note.split("## Raw content")[0].split("\n\n")[3]


# --- story notes ------------------------------------------------------------

def test_story_note_carries_the_multi_outlet_byline():
    note = story_note(item(links=[
        {"publisher": "BleepingComputer", "link": "https://a/1"},
        {"publisher": "Krebs on Security", "link": "https://b/1"},
    ]), "Security Digest", "2026-08-24")
    assert "[BleepingComputer](https://a/1) · [Krebs on Security](https://b/1)" in note


def test_pooled_descriptions_become_one_callout_per_outlet():
    note = story_note(
        item(description="[BleepingComputer] First report.\n\n[Krebs] Second report."),
        "Security Digest", "2026-08-24",
    )
    assert "> [!quote] BleepingComputer" in note
    assert "> [!quote] Krebs" in note
    assert "> First report." in note


def test_a_backfilled_note_says_so_and_has_no_raw_content():
    note = story_note(item(description=""), "Security Digest", "2026-08-24", backfilled=True)
    assert "backfilled: true" in note
    assert "## Raw content" not in note


def test_frontmatter_strings_are_always_quoted():
    """An unquoted title with a colon is a YAML error; one reading `yes` silently
    becomes a boolean. Every string here came from a feed."""
    note = story_note(item(title="Breach: what happened", source="yes"), "D", "2026-08-24")
    assert 'title: "Breach: what happened"' in note


# --- names ------------------------------------------------------------------

def test_note_names_are_date_prefixed_and_slugged():
    assert story_note_name("2026-08-24", "Critical RCE in FortiOS", "https://a/1") == \
        "2026-08-24-critical-rce-in-fortios"


def test_a_same_day_slug_collision_gets_a_hash_but_the_first_keeps_its_name():
    taken = {"2026-08-24-same-title": "https://a/1"}
    assert story_note_name("2026-08-24", "Same title", "https://a/1", taken) == \
        "2026-08-24-same-title"                       # same story, same name
    other = story_note_name("2026-08-24", "Same title", "https://b/2", taken)
    assert other.startswith("2026-08-24-same-title-") and other != "2026-08-24-same-title"


def test_the_same_story_always_gets_the_same_name():
    taken = {"2026-08-24-x": "https://b/2"}
    first = story_note_name("2026-08-24", "x", "https://a/1", taken)
    second = story_note_name("2026-08-24", "x", "https://a/1", taken)
    assert first == second


# --- threshold and linking --------------------------------------------------

def test_one_mention_earns_no_topic_note(tmp_path):
    write_notes([item(entities=["Fortinet"])], DIGEST, CONFIG,
                date="2026-08-24", base=tmp_path, db_path=tmp_path / "d.db")
    assert not list((tmp_path / "topics").glob("*.md"))


def test_the_second_mention_creates_the_note_with_both_stories(tmp_path):
    db = tmp_path / "d.db"
    write_notes([item(entities=["Fortinet"])], DIGEST, CONFIG,
                date="2026-08-24", base=tmp_path, db_path=db)
    write_notes([item(link="https://a/2", title="Another", entities=["Fortinet"])], DIGEST,
                CONFIG, date="2026-08-25", base=tmp_path, db_path=db)

    note = (tmp_path / "topics" / "fortinet.md").read_text()
    assert "security_mentions: 2" in note
    assert "2026-08-24-critical-rce-in-fortios" in note
    assert "2026-08-25-another" in note


def test_an_older_story_is_relinked_when_its_topic_earns_a_note(tmp_path):
    """Otherwise the topic note links to the story and the story does not link
    back -- one edge in the graph where there are two."""
    db = tmp_path / "d.db"
    write_notes([item(entities=["Okta"])], DIGEST, CONFIG,
                date="2026-06-01", base=tmp_path, db_path=db)
    first = tmp_path / "2026" / "06" / "2026-06-01-critical-rce-in-fortios.md"
    assert "**Topics:** Okta" in first.read_text()

    write_notes([item(link="https://a/2", title="Later", entities=["Okta"])], DIGEST, CONFIG,
                date="2026-08-24", base=tmp_path, db_path=db)
    assert "**Topics:** [[okta|Okta]]" in first.read_text()


def test_relink_touches_only_the_topics_line():
    note = story_note(item(), "Security Digest", "2026-08-24")
    after = relink(note, ["Fortinet", "CVE-2026-1234"], {"Fortinet": "fortinet"})
    assert "[[fortinet|Fortinet]]" in after
    # Everything else byte-identical.
    strip = lambda t: [l for l in t.splitlines() if not l.startswith("**Topics:**")]
    assert strip(after) == strip(note)


def test_a_case_variant_counts_as_the_same_thing(tmp_path):
    db = tmp_path / "d.db"
    write_notes([item(entities=["Fortinet"])], DIGEST, CONFIG,
                date="2026-08-24", base=tmp_path, db_path=db)
    write_notes([item(link="https://a/2", title="Two", entities=["FORTINET Inc."])], DIGEST,
                CONFIG, date="2026-08-25", base=tmp_path, db_path=db)
    notes = list((tmp_path / "topics").glob("*.md"))
    assert len(notes) == 1
    assert "security_mentions: 2" in notes[0].read_text()


def test_a_topic_note_keeps_its_filename_once_chosen(tmp_path):
    """The display name moves as spellings accumulate. If the filename followed
    it, the old note -- holding another writer's section and the reader's own
    prose -- would be orphaned in the vault while every link pointed elsewhere."""
    db = tmp_path / "d.db"
    # Two stories spell it "Fortinet" -- the note is created as fortinet.md. Two
    # more spell it "Fortinet Inc.", which ties on count and wins on length, so
    # the display name flips and an unpinned filename would follow it.
    for n, surface in enumerate(["Fortinet", "Fortinet", "Fortinet Inc.", "Fortinet Inc."]):
        write_notes([item(link=f"https://a/{n}", title=f"S{n}", entities=[surface])],
                    DIGEST, CONFIG, date=f"2026-08-2{n}", base=tmp_path, db_path=db)

    assert [p.name for p in (tmp_path / "topics").glob("*.md")] == ["fortinet.md"]
    note = (tmp_path / "topics" / "fortinet.md").read_text()
    assert "security_mentions: 4" in note
    # The heading and title still follow the display name -- only the filename,
    # which links and the merge contract depend on, is pinned.
    assert 'title: "Fortinet Inc."' in note




# --- failure isolation ------------------------------------------------------

def test_a_dead_vault_never_fails_the_run(tmp_path, monkeypatch, caplog):
    """The email has already gone out and history has already recorded it by the
    time the vault is touched. A database asleep on another machine must not turn
    that into a failed pipeline."""
    import src.vault as vault_mod
    from src.vault.livesync import VaultUnavailable

    monkeypatch.setattr(vault_mod, "VAULT_DIR", tmp_path)
    monkeypatch.setattr(
        vault_mod, "project",
        lambda *a, **k: (_ for _ in ()).throw(VaultUnavailable("no route to host")),
    )

    result = vault_mod.project_digest([item()], DIGEST, CONFIG, date="2026-08-24",
                                      db_path=tmp_path / "d.db")

    assert result["projected"] == 0
    assert "no route to host" in result["error"]
    # The notes are still on disk, which is what makes a later resync a catch-up
    # rather than a loss.
    assert list(tmp_path.glob("*/*/*.md"))


def test_a_disabled_vault_does_nothing_at_all(tmp_path, monkeypatch):
    import src.vault as vault_mod

    monkeypatch.setattr(vault_mod, "VAULT_DIR", tmp_path)
    assert vault_mod.project_digest([item()], DIGEST, {"vault": {"enabled": False}}) is None
    assert not list(tmp_path.rglob("*.md"))


def test_only_named_digests_project_when_the_list_is_set():
    from src.vault import enabled_for

    config = {"vault": {"enabled": True, "digests": ["AI News"]}}
    assert enabled_for(config, "AI News")
    assert not enabled_for(config, "Security Digest")
    # Empty means every digest this instance sends.
    assert enabled_for({"vault": {"enabled": True, "digests": []}}, "Security Digest")


# --- environment overrides --------------------------------------------------

def test_env_wins_over_config_for_every_vault_setting(monkeypatch):
    """A VAULT_DB in .env that the code ignored would read as configured while
    the projection quietly went somewhere else."""
    from src.vault import setting

    config = {"vault": {"enabled": False, "db": "from-config", "user": "u", "folder": "f"}}
    monkeypatch.setenv("VAULT_ENABLED", "true")
    monkeypatch.setenv("VAULT_DB", "from-env")
    monkeypatch.setenv("VAULT_USER", "env-user")

    assert setting(config, "enabled") is True
    assert setting(config, "db") == "from-env"
    assert setting(config, "user") == "env-user"
    assert setting(config, "folder") == "f"      # not in env, config still used


def test_an_empty_env_var_does_not_shadow_config(monkeypatch):
    from src.vault import setting

    monkeypatch.setenv("VAULT_DB", "   ")
    assert setting({"vault": {"db": "real"}}, "db") == "real"


def test_no_database_named_is_refused_rather_than_guessed(monkeypatch):
    """Guessing means a 404 at best and writing into another application's
    database at worst."""
    from src.vault import build_vault
    from src.vault.livesync import VaultUnavailable

    monkeypatch.delenv("VAULT_DB", raising=False)
    monkeypatch.setenv("VAULT_COUCHDB_URL", "http://couch.test")
    monkeypatch.setenv("VAULT_COUCHDB_PASSWORD", "pw")
    with pytest.raises(VaultUnavailable, match="No vault database named"):
        build_vault({"vault": {"enabled": True}})


# --- the shared-vault contract (~/.claude/skills/obsidian-vault-writer) ------

def test_generated_notes_are_marked_as_machine_managed():
    """The vault's cross-project convention: a managed note carries `source:`.
    homelab/vault-sync.sh refuses to overwrite a note that lacks one."""
    assert "source: security-digest" in story_note(item(), "Security Digest", "2026-08-24")


def test_the_news_outlet_does_not_squat_on_the_managed_marker():
    """`source: "BleepingComputer"` would make a generated note claim to have
    been synced from a news site."""
    note = story_note(item(publisher="BleepingComputer"), "Security Digest", "2026-08-24")
    assert 'publisher: "BleepingComputer"' in note
    assert 'source: "BleepingComputer"' not in note


def test_topic_notes_are_not_marked_managed():
    """They are shared. Claiming a note another writer and the reader also own
    would invite exactly the wholesale overwrite the contract forbids."""
    from src.vault.topics import note_body

    body = note_body("fortinet", [{"surface": "Fortinet", "story_note": "s",
                                   "story_title": "T", "digest_title": "D",
                                   "published": "2026-08-24"}])
    assert "source: security-digest" not in body


def test_identical_input_renders_byte_identically(tmp_path):
    """A vault replicating over both iCloud and LiveSync re-syncs every note it
    is handed, so a run that changes nothing must produce nothing."""
    db = tmp_path / "d.db"
    write_notes([item()], DIGEST, CONFIG, date="2026-08-24", base=tmp_path, db_path=db)
    first = {p: p.read_text() for p in tmp_path.rglob("*.md")}

    write_notes([item()], DIGEST, CONFIG, date="2026-08-24", base=tmp_path, db_path=db)
    assert {p: p.read_text() for p in tmp_path.rglob("*.md")} == first


def test_no_generated_timestamp_anywhere_in_a_note():
    """A "generated at" line replicates the whole corpus on every run."""
    import datetime

    note = story_note(item(), "Security Digest", "2026-08-24")
    now = datetime.datetime.now()
    assert now.strftime("%H:%M") not in note
    assert str(now.year) + "-" + now.strftime("%m-%d") not in note.replace("2026-08-24", "")


# --- cross-app name adoption ------------------------------------------------

def test_a_name_another_writer_already_chose_is_adopted(tmp_path):
    """podcast-digest pins into its own database and we pin into ours; neither
    can read the other. The vault is the only store both can see, so a thing that
    already has a note there keeps that one name instead of gaining a second.

    Fires when a spelling our corpus has actually used matches the existing
    filename -- here ours would have picked `fortinet` (2 sightings beat 1), but
    it has also seen "Fortinet Inc." and that is what already has a note."""
    db = tmp_path / "d.db"
    write_notes(
        [item(entities=["Fortinet"]), item(link="https://a/2", title="Two", entities=["Fortinet"]),
         item(link="https://a/3", title="Three", entities=["Fortinet Inc."])],
        DIGEST, CONFIG, date="2026-08-24", base=tmp_path, db_path=db,
        existing_topics={"fortinet-inc", "anthropic"},
    )
    assert [p.name for p in (tmp_path / "topics").glob("*.md")] == ["fortinet-inc.md"]


def test_spellings_we_have_never_seen_are_not_guessed_at(tmp_path):
    """The honest limit. If the two corpora share no spelling of a thing -- ours
    only ever says "Volt", theirs only "Volt Typhoon" -- adoption cannot fire,
    and the topic gets two notes. Closing that would mean matching on something
    looser than an exact filename, which risks merging two unrelated topics onto
    one page: worse than the split, and far harder to notice."""
    from src.vault.topics import _adopt

    assert _adopt({"Volt": 2}, "volt", {"volt-typhoon"}) is None


def test_the_most_common_spelling_is_tried_first(tmp_path):
    from src.vault.topics import _adopt

    surfaces = {"Volt": 1, "Volt Typhoon": 5}
    assert _adopt(surfaces, "volt typhoon", {"volt", "volt-typhoon"}) == "volt-typhoon"


def test_adoption_requires_an_exact_match(tmp_path):
    """Adopting the wrong note merges two unrelated topics onto one page -- worse
    than the split it prevents, and far harder to notice."""
    from src.vault.topics import _adopt

    assert _adopt({"Fortinet": 2}, "fortinet", {"fortiguard", "fortios"}) is None
    assert _adopt({"Fortinet": 2}, "fortinet", set()) is None
    assert _adopt({"Fortinet": 2}, "fortinet", None) is None


def test_an_adopted_name_is_pinned_like_any_other(tmp_path):
    """Adoption happens once. If the other writer's note were later deleted, we
    must not silently rename ours out from under every link pointing at it."""
    db = tmp_path / "d.db"
    for n in range(2):
        write_notes([item(link=f"https://a/{n}", title=f"S{n}", entities=["Fortinet Inc."])],
                    DIGEST, CONFIG, date=f"2026-08-2{n}", base=tmp_path, db_path=db,
                    existing_topics={"fortinet-inc"})
    assert (tmp_path / "topics" / "fortinet-inc.md").exists()

    write_notes([item(link="https://a/9", title="S9", entities=["Fortinet"])],
                DIGEST, CONFIG, date="2026-08-29", base=tmp_path, db_path=db,
                existing_topics=set())        # the other note is gone now
    assert [p.name for p in (tmp_path / "topics").glob("*.md")] == ["fortinet-inc.md"]


def test_an_unreachable_vault_still_lets_us_name_topics(monkeypatch):
    """Not knowing what is in the vault is a reason to choose our own name, never
    a reason to fail a digest that has already been delivered."""
    import src.vault as vault_mod
    from src.vault.livesync import VaultUnavailable

    monkeypatch.setattr(
        vault_mod, "build_vault",
        lambda cfg: (_ for _ in ()).throw(VaultUnavailable("down")),
    )
    assert vault_mod.existing_topics({"vault": {"enabled": True}}) == set()


# --- year/month layout ------------------------------------------------------

def test_a_story_note_is_filed_under_its_year_and_month():
    from src.vault.render import story_dir, story_rel

    assert story_dir("2026-08-25") == "2026/08"
    assert story_rel("2026-08-25-fortinet-rce") == "2026/08/2026-08-25-fortinet-rce.md"


def test_a_stem_without_a_date_is_refused_not_mangled():
    """Better to skip a note we cannot place than to build a path out of a slice
    of a slug -- `2026/08` would become something like `fort/ne`."""
    from src.vault.render import story_rel

    for stem in ("fortinet-rce", "2026-08", "", "20260825-x", "2026-8-5-x"):
        assert story_rel(stem) is None, stem


def test_writing_creates_a_month_folder_and_no_flat_one(tmp_path):
    write_notes([item()], DIGEST, CONFIG, date="2026-08-24",
                base=tmp_path, db_path=tmp_path / "d.db")

    assert (tmp_path / "2026" / "08" / "2026-08-24-critical-rce-in-fortios.md").is_file()
    assert not (tmp_path / "stories").exists()


def test_two_months_land_in_two_folders(tmp_path):
    db = tmp_path / "d.db"
    write_notes([item()], DIGEST, CONFIG, date="2026-07-31", base=tmp_path, db_path=db)
    write_notes([item(link="https://a/2", title="Two")], DIGEST, CONFIG,
                date="2026-08-01", base=tmp_path, db_path=db)

    assert len(list((tmp_path / "2026" / "07").glob("*.md"))) == 1
    assert len(list((tmp_path / "2026" / "08").glob("*.md"))) == 1


def test_a_same_day_collision_is_still_resolved_within_its_month(tmp_path):
    """claimed_stems only scans the run's own month, which is sound because a
    stem carries its date -- but it must still catch a collision inside it."""
    db = tmp_path / "d.db"
    write_notes(
        [item(title="Same title"), item(link="https://a/2", title="Same title")],
        DIGEST, CONFIG, date="2026-08-24", base=tmp_path, db_path=db,
    )
    names = sorted(p.stem for p in (tmp_path / "2026" / "08").glob("*.md"))
    assert len(names) == 2
    assert names[0] == "2026-08-24-same-title"
    assert names[1].startswith("2026-08-24-same-title-")


def test_relink_reaches_a_story_in_a_different_month(tmp_path):
    """The regression the old flat folder hid: a topic crossing the threshold
    today has to relink stories written months ago, which live elsewhere now."""
    db = tmp_path / "d.db"
    write_notes([item(entities=["Okta"])], DIGEST, CONFIG,
                date="2026-06-01", base=tmp_path, db_path=db)
    june = tmp_path / "2026" / "06" / "2026-06-01-critical-rce-in-fortios.md"
    assert "**Topics:** Okta" in june.read_text()

    written = write_notes([item(link="https://a/2", title="Later", entities=["Okta"])],
                          DIGEST, CONFIG, date="2026-08-24", base=tmp_path, db_path=db)

    assert "**Topics:** [[okta|Okta]]" in june.read_text()
    # And the relinked note is reported, so the projection pushes it.
    assert "2026/06/2026-06-01-critical-rce-in-fortios.md" in written


# --- prune guards -----------------------------------------------------------

class _Vault:
    """A stand-in that records what would be deleted."""

    def __init__(self, entries):
        self.entries = set(entries)
        self.deleted = []

    def vault_path(self, relative):
        rel = str(relative)
        if rel.startswith("topics/"):
            return f"99 topics/{rel.split('/', 1)[1]}"
        return f"12 daily-digest/{rel}"

    def entries_under(self, prefix):
        return {e for e in self.entries if e.startswith(prefix)}

    def soft_delete(self, path):
        self.deleted.append(path)
        return True

    def close(self):
        pass


def _prune_with(monkeypatch, entries, paths, tmp_path):
    import src.vault as vault_mod

    fake = _Vault(entries)
    monkeypatch.setattr(vault_mod, "build_vault", lambda cfg: fake)
    removed = vault_mod._prune(paths, {"vault": {"enabled": True}}, tmp_path)
    return fake, removed


def test_prune_removes_what_is_no_longer_produced(monkeypatch, tmp_path):
    fake, removed = _prune_with(
        monkeypatch,
        {"12 daily-digest/2026/08/a.md", "12 daily-digest/stories/a.md"},
        ["2026/08/a.md"],
        tmp_path,
    )
    assert removed == ["12 daily-digest/stories/a.md"]
    assert fake.deleted == ["12 daily-digest/stories/a.md"]


def test_prune_never_touches_the_shared_topics_folder(monkeypatch, tmp_path):
    """Our disk copy of a topic note is only our *section* of it, so "not on
    disk" says nothing about whether the note should exist. Deleting there would
    take another writer's work and the reader's own prose with it."""
    fake, removed = _prune_with(
        monkeypatch,
        {"12 daily-digest/2026/08/a.md", "99 topics/fortinet.md", "99 topics/okta.md"},
        ["2026/08/a.md"],           # no topics/ on disk at all
        tmp_path,
    )
    assert removed == []
    assert fake.deleted == []


def test_prune_refuses_when_nothing_is_on_disk(monkeypatch, tmp_path, caplog):
    """What a missing output mount looks like. Without this guard it would
    delete every note we have ever written."""
    fake, removed = _prune_with(
        monkeypatch, {"12 daily-digest/2026/08/a.md"}, [], tmp_path,
    )
    assert removed == []
    assert fake.deleted == []


def test_resync_does_not_prune_unless_asked(monkeypatch, tmp_path):
    import src.vault as vault_mod

    (tmp_path / "2026" / "08").mkdir(parents=True)
    (tmp_path / "2026" / "08" / "a.md").write_text("x")
    monkeypatch.setattr(vault_mod, "project", lambda *a, **k: {"considered": 1, "projected": 1, "skipped": 0})
    called = []
    monkeypatch.setattr(vault_mod, "_prune", lambda *a, **k: called.append(1) or [])

    assert "pruned" not in vault_mod.resync({}, base=tmp_path)
    assert called == []
    assert vault_mod.resync({}, base=tmp_path, prune=True)["pruned"] == []
    assert called == [1]
