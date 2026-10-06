"""Music CSV regressions: recording identity, meaningful suggestions and source rows."""
import harness

harness.setup()

from app import imports, jellyfin, media, songs, wants

check = harness.Check("music import fixes")

def hit(title, artist="Example Artist", **extra):
    return dict(title=title, artist=artist, unit="track", medium="music",
                itemKey="test:" + title, **extra)

def exact(title, other, artist="Example Artist", credit="Example Artist"):
    return imports.is_strict(imports.Row(2, title, artist), hit(other, credit), media.MUSIC)

for a, b in [
    ("Rock and Roll", "Rock N Roll"), ("Rock and Roll", "Rock N' Roll"),
    ("Rock and Roll", "Rock 'n' Roll"),
    ("whatawonderfulworld", "What a Wonderful World"),
    ("What_a_Wonderful_World", "What a Wonderful World"),
    ("What%20a%20Wonderful%20World", "What a Wonderful World"),
    ("When the Morning Turns to Night / What the Fuck Are We Saying?",
     "When Morning Turns To Night/What The Fuck Are We Saying"),
    ("Song - Single", "Song"), ("Song (Single)", "Song"),
    ("Song Single", "Song"), ("Song (2011 Remaster)", "Song"),
    ("Song - Live", "Song (Live)"), ("Любовь", "Любовь"),
]:
    check.that(exact(a, b) and exact(b, a), f"the same recording: {a!r}, {b!r}")

for credit in ("Smith John", "by Smith John"):
    check.that(exact("Song", "Song", "John Smith", credit), "first/last names may be reversed")
check.that(exact("Song", "Song", "Main feat. John Smith", "Main feat. Smith John"),
           "featured names may be reversed in artist metadata")
check.that(exact("Song feat. John Smith", "Song feat. Smith John"),
           "unparenthesized featured names may be reversed")
check.that(exact("Song (feat. John Smith)", "Song", "Main", "Main feat. Smith John"),
           "a featured name may move between title and artist columns")

for title in ("Song (Live)", "Song (Acoustic)", "Song (Kito Remix)",
              "Song (Demo)", "Song (Rehearsal Version)", "Song (Single Edit)",
              "Song (Single Version)", "Song (Taylor's Version)",
              "Song (Live, 2011 Remaster)", "Song 2"):
    check.that(not exact("Song", title), f"different recording or title: {title}")
check.that(not exact("Stand By Me", "Stand"), "by inside a title is not a credit marker")
check.that(not exact("Song feat. John Smith", "Song feat. Jane Doe"), "conflicting guests are not exact")
check.that(not exact("Song", "Song", "Belle and Sebastian", "Sebastian"), "keep whole band names")
check.that(not exact("Song", "Song", "John Smith Band", "Band Smith John"), "do not sort arbitrary names")
check.that(not exact("Song", "Song", "", "Example Artist"), "no artist is not an exact identity")
check.that(not exact("Song", "Song", "Different Artist", "Example Artist"), "same title alone is insufficient")

csv = ('Track name,Artist name,Album,Type\n'
       'Song,Example Artist,"Album\nwith a newline",track\n'
       'An Album,Example Artist,An Album,album\n'
       'Example Artist,,,artist\n'
       '\n'
       'Song (Live),Example Artist,A Live Album,song\n'
       'Song (Acoustic),Example Artist,An Album,track\n'
       'Song,Example Artist,An Album,track\n'
       'Unknown,Somebody,,unrecognized\n')
skipped = []
rows, duplicates, blanks = imports.rows(imports.read(csv), media.MUSIC, "track", skipped)
check.equal([r.title for r in rows], ["Song", "Song (Live)", "Song (Acoustic)"],
            "albums/artists are excluded and distinct versions survive")
check.equal([r.source_row for r in rows], [2, 6, 7], "spreadsheet record numbers count empty rows once")
check.equal([(r.line_start, r.line) for r in rows], [(2, 3), (7, 7), (8, 8)],
            "physical line ranges remain separate and stable")
check.equal(duplicates, 1, "only the repeat recording is discarded")
check.equal(len(skipped), 3, "other and unknown types are reported")
check.equal(rows[0].album, "Album\nwith a newline", "album metadata is retained")
check.equal(imports.source_label(dict(line=3, line_start=2, source_row=2)),
            "Row 2 of your file (physical lines 2–3)", "the visible reference explains both numbers")
check.raises(imports.Unreadable,
             lambda: imports.rows(imports.read("Artist,Album\nBand,Album\n"), media.MUSIC, "track"),
             "an album column is never silently substituted for missing tracks")
check.raises(imports.Unreadable,
             lambda: imports.read('Track,Artist\n"Unclosed,Artist\n'), "unclosed quotes are refused")
check.raises(imports.Unreadable,
             lambda: imports.read('Track,Artist\nSong,With,Comma\n'), "shifted extra columns are refused")

user = jellyfin.User(id="test", name="test", is_admin=False)
row = imports.Row(220, "Crash And Burn / Subway", "Sheryl Crow")
unrelated = hit("Sheryl Crow", "Tim McGraw", owned=True)
wants.search = lambda *args, **kwargs: [unrelated]
check.equal(imports.match(user, media.MUSIC, "track", row, set())["state"], imports.MISSING,
            "an unrelated owned result does not resolve or become a close match")
better = hit("Crash and Burn / Subway Ride", "Sheryl Crow")
wants.search = lambda *args, **kwargs: [unrelated, better]
matched = imports.match(user, media.MUSIC, "track", row, set())
check.equal((matched["state"], matched["hit"]["title"]), (imports.UNCERTAIN, better["title"]),
            "a plausible later hit wins over the unrelated first result")
wants.search = lambda *args, **kwargs: [hit("Song (Live)", owned=True)]
check.equal(imports.match(user, media.MUSIC, "track", imports.Row(2, "Song", "Example Artist"), set())["state"],
            imports.UNCERTAIN, "an owned version mismatch still needs a decision")
check.equal(imports._query(imports.Row(2, "Hearts%20of_Lions - Single", "Atura"), "music", "track"),
            "Atura Hearts of Lions", "query encoding/release labels are normalized too")

index = songs.LibraryIndex([{"Id": "one", "Name": "What a Wonderful World", "Artists": ["John Smith"]},
                            {"Id": "live", "Name": "Song (Live)", "Artists": ["John Smith"]}])
check.equal(index.find("whatawonderfulworld", "Smith John"), "one", "library lookup uses the same normalization")
check.equal(index.find("Song", "Smith John"), None, "library lookup still rejects the live recording")

harness.cleanup()
raise SystemExit(check.report())
