"""How a genre is made, for the model to write from: tempo, feel, chords, the drum
pattern, the bass, the sounds, the form, the production.

A model knows a genre by name and mixes its details with its neighbours'. A guide puts
the conventions in front of it in this tool's own terms: the drum pattern as a grid and
as the notes of a `drums` track, the sounds as presets and designed synths the tool has.
They are conventions, read from the sources each guide names (`STYLES`), not rules, and
not a measurement of anything here: whether a piece written from one sounds like the
genre is for the person's ears.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

#: The kit's drums a pattern may name, in the order its rows are shown.
_ROWS = (
    "kick", "kick_hard", "snare", "snare_electro", "clap", "rim", "sidestick", "hat",
    "pedalhat", "openhat", "ride", "crash", "shaker", "clave", "tom",
)


@dataclass(frozen=True, slots=True)
class Style:
    name: str
    #: Other names the person may use, Spanish ones included.
    also: tuple[str, ...]
    tempo: str
    feel: str
    harmony: str
    #: Chord loops as degrees of the key, one bar a chord unless they say otherwise.
    progressions: tuple[str, ...]
    #: One bar of drums: drum name to its steps, four a beat, `x` a hit and `.` none.
    drums: Mapping[str, str]
    bass: str
    #: The defining sounds, as this tool makes them.
    sounds: str
    form: str
    production: str
    #: The drums as notes, where a grid cannot say them (swing, a two-bar cell).
    drums_notes: str = ""
    #: What the pattern needs said beside it.
    drums_note: str = ""
    #: Where the conventions were read on 2026-11-18, and which fields each supports.
    sources: tuple[str, ...] = field(default=())
    #: What in the guide is common practice written without a source: a hypothesis.
    unsourced: str = ""


_GRID = "x.x.x.x.x.x.x.x."
_FOUR = "x...x...x...x..."
_BACK = "....x.......x..."
_OFF = "..x...x...x...x."

STYLES: tuple[Style, ...] = (
    Style(
        name="synthwave", also=("outrun", "retrowave", "80s synth"),
        tempo="80-118 bpm; the faster outrun kind 128-140.",
        feel="4/4, straight.",
        harmony="Minor for the brooding kind, major for the pop kind. Chords held a bar or two.",
        progressions=("i VI VII VI", "i VI III VII", "i iv VII III"),
        drums={"kick": "x.......x.......", "snare": _BACK, "hat": _GRID},
        drums_note="For the driving kind put the kick on every beat. A tom fill ends an "
        "8-bar phrase.",
        bass="The chord's root repeated on every eighth, an octave or two under the chords.",
        sounds="Bass: a `sound` of two saws, one detuned, lowpass with resonance and an "
        "envelope on the cutoff. Pad: detuned saws with a slow attack (preset `WarmStack`). "
        "Lead: a bright saw or square (preset `SuperSawLead`). An arpeggio of the chord in "
        "sixteenths. Drums: the kit with `snare_electro`.",
        form="Intro 8 (drums and bass), verse 16, chorus 8, verse 16, chorus 8, outro 8. The "
        "beat starts as kick and snare; hats and the arpeggio come in later.",
        production="Reverb on the snare, cut short. Chorus or flanger on pads and leads. A "
        "slow `automation` of the bass cutoff.",
        sources=(
            "modeaudio.com/magazine/synthwave-5-production-essentials (tempo, drums, bass, "
            "sounds, production)",
            "emastered.com/blog/synthwave-chord-progressions (the first progression)",
            "en.wikipedia.org/wiki/Synthwave (tempo)",
        ),
        unsourced="the snare on 2 and 4 (sources disagreed), the second and third "
        "progressions, the form's bar counts",
    ),
    Style(
        name="lo-fi hip hop", also=("lofi", "lo-fi", "lofi hip hop", "chillhop"),
        tempo="60-90 bpm, most around 75-85.",
        feel="4/4, swung and loose: the off-beat hat falls late.",
        harmony="Jazz chords: sevenths and ninths. Two to four chords, each a bar or two. "
        "A triad plus the note a seventh above its root makes the seventh chord.",
        progressions=("ii V I I", "I vi ii V", "i iv i iv"),
        drums={"kick": "x......x..x.....", "rim": _BACK, "hat": _GRID},
        drums_notes="[kick hat]:0.6 hat:0.4 [rim hat]:0.6 hat:0.15 kick:0.25 hat:0.6 "
        "[kick hat]:0.4 [rim hat]:0.6 hat:0.4",
        drums_note="Few elements, soft: `kick_soft`, `rim` or `sidestick`, hats at volume "
        "60-80.",
        bass="The chord's root, simple and low, with long notes.",
        sounds="Keys: `electric_piano` or `piano`, with a lowpass. A `guitar` line. Bass: "
        "`acoustic_bass` or the `sub_bass` synth. A noise track at low volume for vinyl "
        "crackle (a `sound` with a noise oscillator through a bandpass).",
        form="A 4- or 8-bar loop that repeats; layers come in and out every 8 bars. 2-3 "
        "minutes.",
        production="A lowpass around 2,000 Hz on the keys or the whole song (`eq` highcut). "
        "Bitcrush lightly on the drums. No bright sounds.",
        sources=(
            "modeaudio.com/magazine/lofi-hip-hop-5-production-essentials (tempo, drums, "
            "bass, sounds, production)",
            "splice.com/blog/lo-fi-chord-progressions (chord types, ii V I)",
            "en.wikipedia.org/wiki/Lo-fi_hip_hop (sounds)",
        ),
        unsourced="the exact kick placement, the swing amount, the form",
    ),
    Style(
        name="boom bap", also=("boombap", "90s hip hop", "old school hip hop"),
        tempo="85-95 bpm.",
        feel="4/4, swung: off-beat hats late, as from a sampler's swing.",
        harmony="A short loop in the manner of a jazz or soul sample: two to four chords, "
        "minor sevenths common.",
        progressions=("i iv", "i VI ii V", "ii V I vi"),
        drums={"kick": "x.....x...x.....", "snare": _BACK, "hat": _GRID},
        drums_notes="[kick hat]:0.57 hat:0.43 [snare hat]:0.57 [kick hat]:0.43 [kick hat]:0.57 "
        "hat:0.43 [snare hat]:0.57 hat:0.43",
        drums_note="Hard kick and snare (`kick_hard`, `snare_acoustic`), loud in the mix. "
        "Vary the hats' volume.",
        bass="A plain, round bass on the chord's root, following the kick.",
        sounds="The loop: `electric_piano`, `piano`, `strings` or a `sample` cut into "
        "`slices` and reordered. Bass: the `sub_bass` synth. Drums up front.",
        form="Intro 4, verse 16, hook 8, verse 16, hook 8. The loop drops out for a bar "
        "before a section starts.",
        production="Sparse: drums, bass, one loop. Bitcrush or a lowpass on the loop for "
        "the sampled sound.",
        sources=(
            "en.wikipedia.org/wiki/Boom_bap (drums, bass, sounds, production)",
            "attackmagazine.com/technique/beat-dissected/90s-boom-bap-hip-hop (swing, hats, "
            "bass)",
        ),
        unsourced="the tempo range (from search snippets), the progressions, the form",
    ),
    Style(
        name="trap", also=("trap music", "808"),
        tempo="130-150 bpm written in half time (it feels like 65-75).",
        feel="4/4, half time: the clap falls on beat 3. Hats move faster than the pulse.",
        harmony="Minor, dark, few chords, each held two bars. A short repeated melody.",
        progressions=("i i VI VI", "i VI VII i", "i iv i iv"),
        drums={"kick": "x.............x.", "clap": "........x.......", "hat": _GRID},
        drums_note="Hat rolls are the signature: replace one or two hats with "
        "`hat:0.125 hat:0.125`, or with three `hat:1/12`. The next bar's kick moves, e.g. "
        "to the 'and' of beat 1.",
        bass="A long, deep bass note on the chord's root with every kick: the 808. "
        "Register C1-G2.",
        sounds="Bass: the `sub_bass` synth with a long release, or a kick `sample` played "
        "at pitches. Melody: `bell`, `pluck` or `piano`, high and sparse. `strings` or "
        "`brass` for weight.",
        form="A 4- or 8-bar loop: intro 8, hook 8, verse 16, hook 8. Drums drop out for a "
        "bar before the hook.",
        production="Minimal and heavy at the bottom. The bass loud and clean; distortion on "
        "it for grit.",
        sources=(
            "native-instruments.com/blogs/blog/how-to-make-a-trap-beat (tempo, drums, bass)",
            "en.wikipedia.org/wiki/Trap_music (tempo, feel, sounds, production)",
        ),
        unsourced="the progressions, the form",
    ),
    Style(
        name="house", also=("deep house", "classic house"),
        tempo="115-130 bpm, usually 120-128.",
        feel="4/4, straight, the kick on every beat.",
        harmony="Minor or major sevenths, played as short stabs or held pads. Two to four "
        "chords.",
        progressions=("i VII VI VII", "i iv", "ii V I I"),
        drums={"kick": _FOUR, "clap": _BACK, "hat": _FOUR, "openhat": _OFF},
        bass="A short repeated line that sits between the kicks, on the off-beats, around "
        "the chord's root.",
        sounds="Chord stabs: `electric_piano` or `organ`, or a `sound` of saws with a short "
        "decay. Bass: the `bass` synth or the LB302 preset `STrash`'s kind (action sounds, "
        "query LB302). Optional `shaker` sixteenths.",
        form="In 8-bar blocks: intro 16 (drums), then a layer every 8 bars, breakdown 16 "
        "without the kick, drop 32, outro 16.",
        production="Reverb on the clap. A filter that opens over 8 or 16 bars before a "
        "drop (`automation` of a cutoff).",
        sources=(
            "en.wikipedia.org/wiki/House_music (tempo, feel, drums, bass, sounds, form)",
            "musicradar.com/news/beat-building-how-to-make-classic-house (tempo, drums, "
            "production)",
        ),
        unsourced="the progressions, the bar counts of the form",
    ),
    Style(
        name="techno", also=("tech", "minimal techno"),
        tempo="125-140 bpm.",
        feel="4/4, straight or lightly swung, the kick on every beat.",
        harmony="Little: one note or one chord repeated, often minor. Rhythm and sound "
        "carry the piece.",
        progressions=("i (one chord throughout)", "i i VII i"),
        drums={"kick": _FOUR, "clap": _BACK, "hat": _OFF, "shaker": "xxxxxxxxxxxxxxxx"},
        drums_note="The shaker at volume 40-60. A soft extra kick just before beats 2 and 4 "
        "is common.",
        bass="A short pattern of one or two notes, off the kick by an eighth or sixteenth.",
        sounds="A repeated synth note with its filter moving (`automation` of the cutoff). "
        "Presets `TB303` and LB302's for acid lines. Toms as rhythm.",
        form="In 8- and 16-bar phrases: start with the kick, add a layer every 8 bars, take "
        "the kick out for 8-16, bring everything back.",
        production="The kick loud and tuned to the piece's key note. Distortion and delay "
        "on the lead pattern. Few melodies.",
        sources=(
            "en.wikipedia.org/wiki/Techno (tempo, feel, harmony, sounds)",
            "attackmagazine.com/technique/beat-dissected/rolling-techno (tempo, swing, "
            "drums, production)",
        ),
        unsourced="the clap and off-beat hat (a search snippet), the bass, the form",
    ),
    Style(
        name="trance", also=("uplifting trance", "psytrance"),
        tempo="125-150 bpm, most often 138.",
        feel="4/4, straight, the kick on every beat.",
        harmony="Minor. Long chords under fast arpeggios.",
        progressions=("i VI III VII", "IV I V vi"),
        drums={"kick": _FOUR, "clap": _BACK, "openhat": _OFF},
        drums_note="A snare roll (eighths, then sixteenths) builds into the drop.",
        bass="A note exactly between each two kicks, on the off-beat, on the chord's root.",
        sounds="Lead: many detuned saws (preset `SuperSawLead`), playing the chord as an "
        "arpeggio in sixteenths. Pad: detuned saws, long. A `piano` in the breakdown.",
        form="Intro 16, build 16, breakdown 16-32 without drums or bass, build 16 with the "
        "snare roll, climax 32, outro 16. Something changes every 8 or 16 bars.",
        production="Long reverb and delay on the lead. A filter opening through the "
        "build. The pad cut in a rhythm (short notes instead of held ones).",
        sources=(
            "en.wikipedia.org/wiki/Trance_music (tempo, harmony, drums, sounds, form)",
            "musicradar.com/tuition/tech/26-trance-tips-34006 (drums, bass, sounds, form)",
        ),
        unsourced="the progressions (search snippets), the bar counts",
    ),
    Style(
        name="drum and bass", also=("dnb", "d&b", "jungle", "liquid"),
        tempo="165-180 bpm, usually 174.",
        feel="4/4. The drums are fast; kick and snare feel like half that speed.",
        harmony="Liquid: jazz chords and melody. Darker kinds: one minor chord and "
        "dissonant pads.",
        progressions=("i VI III VII", "i iv", "ii V I I"),
        drums={"kick": "x.........x.....", "snare": _BACK, "hat": _GRID},
        drums_note="Extra soft snares between the main ones make it roll.",
        bass="A deep sub on the root with long notes, and above it a moving, buzzing bass "
        "(preset `DirtyReece`, or two detuned saws through a lowpass).",
        sounds="The kit's `snare_acoustic` and `kick_hard`, or a break `sample` cut into "
        "`slices`. `sub_bass`. Pads and an `electric_piano` for the liquid kind.",
        form="Intro 32, drop 48, breakdown 16, second drop 48, outro 16, in 16-bar "
        "sections.",
        production="Sparse and wide in frequency: a clean sub, crisp drums, room in "
        "between.",
        sources=(
            "en.wikipedia.org/wiki/Drum_and_bass (tempo, harmony, drums, bass, sounds)",
            "native-instruments.com/blogs/blog/drum-and-bass (tempo, drums, bass, form)",
        ),
        unsourced="the progressions",
    ),
    Style(
        name="dubstep", also=("brostep", "wobble"),
        tempo="132-142 bpm, most at 140.",
        feel="4/4, half time: the snare on beat 3. Syncopated, often shuffled.",
        harmony="Minor or Phrygian, dissonant: the flat second and the tritone. Few chords.",
        progressions=("i (one chord, the bass moves)", "i bII i bII"),
        drums={"kick": "x...............", "snare": "........x.......", "hat": _FOUR},
        drums_note="Sparse. A second kick late in the bar is common.",
        bass="A long low note whose tone moves in rhythm: the wobble. Write it as a held "
        "note with an LFO on the cutoff (`sound` filter lfo, rate 2-8 Hz) or `automation` "
        "of the cutoff.",
        sounds="Bass: saw and square through a resonant lowpass, with distortion. A "
        "`sub_bass` under it. Dark pads, reverberant drums.",
        form="Intro 16 or 32, drop 32, middle 16, second drop 32, outro 16.",
        production="Sparse, with a strong sub. Long reverb on the snare.",
        sources=("en.wikipedia.org/wiki/Dubstep (all sourced fields)",),
        unsourced="the kick and hat placement, the progressions",
    ),
    Style(
        name="ambient", also=("soundscape", "paisaje sonoro", "drone", "ambiental"),
        tempo="60-90 bpm, only as a grid: there is little or no beat.",
        feel="No pulse to speak of. Tone and space before rhythm.",
        harmony="One scale or one chord for a long time, often a drone on the key note "
        "with slow changes over it.",
        progressions=("i (a drone)", "i VI III VII, each 4 bars or more"),
        drums={},
        drums_note="No drums. If any, soft and far: a `shaker` or `tom` now and then.",
        bass="A warm, simple low note that moves rarely: the `sub_bass` synth or a sine.",
        sounds="Long evolving pads (a `sound` of detuned saws, attack 3-5 s, release 5-8 "
        "s, a lowpass with a slow LFO). Sparse `bell` or `piano` notes. A noise track "
        "through a bandpass for air.",
        form="No verses: one idea that grows. Layers enter one at a time over 8-16 bars "
        "and leave the same way.",
        production="Long reverb on everything, delay on the sparse notes, slow "
        "`automation` of cutoffs and of the master volume for the fade in and out.",
        sources=(
            "soundonsound.com/techniques/sound-design-ambient-music (tempo, harmony, bass, "
            "sounds, form, production)",
            "en.wikipedia.org/wiki/Ambient_music (feel, harmony)",
        ),
        unsourced="the second progression",
    ),
    Style(
        name="pop", also=("pop music", "synth pop", "electropop"),
        tempo="100-130 bpm.",
        feel="4/4, straight.",
        harmony="Major or minor, plain triads, one chord a bar. The same four chords can "
        "carry verse and chorus.",
        progressions=("I V vi IV", "vi IV I V", "I vi IV V"),
        drums={"kick": "x.......x.......", "snare": _BACK, "hat": _GRID},
        bass="The chord's root on the beat, following the kick.",
        sounds="`piano` or `guitar` for chords, a synth pad, the `bass_guitar` or `bass` "
        "synth, a clear lead that would be the voice.",
        form="Intro 4-8, verse 16, pre-chorus 4-8, chorus 8, verse 16, chorus 8, bridge 8, "
        "chorus 8-16. The chorus is the fullest part.",
        production="A clear lead on top. Reverb and delay kept tidy. Compression on the "
        "whole song.",
        sources=(
            "hooktheory.com/blog/i-analyzed-the-chords-of-1300-popular-songs-for-patterns"
            "-this-is-what-i-found (I V vi IV)",
            "en.wikipedia.org/wiki/Pop_music (sounds, the verse-chorus form)",
        ),
        unsourced="the tempo, the drums, the bass, the other progressions, the bar counts",
    ),
    Style(
        name="rock", also=("rock music", "hard rock", "indie rock"),
        tempo="110-140 bpm.",
        feel="4/4, straight, the snare on beats 2 and 4.",
        harmony="Triads and power chords (root and fifth only). Melodies in the Dorian or "
        "Mixolydian mode are common.",
        progressions=("I IV V IV", "I bVII IV I", "i VI VII i"),
        drums={"kick": "x.......x.x.....", "snare": _BACK, "hat": _GRID},
        drums_note="A `crash` on the first beat of a section; `ride` in place of the hat "
        "in the chorus.",
        bass="The chord's root in steady eighths, locked to the kick.",
        sounds="`overdrive_guitar` or `distortion_guitar` playing power chords, "
        "`electric_guitar` for the lead, `bass_guitar`, the kit with `snare_acoustic`.",
        form="Intro 4-8, verse 16, chorus 8, verse 16, chorus 8, solo 8-16, chorus 16.",
        production="Distortion on the guitars, a little room reverb, the two guitar "
        "tracks panned left and right.",
        sources=(
            "en.wikipedia.org/wiki/Rock_music (feel, harmony, sounds, the verse-chorus form)",
            "en.wikipedia.org/wiki/Drum_beat (kick and snare)",
        ),
        unsourced="the tempo, the progressions, the bass, the bar counts",
    ),
    Style(
        name="funk", also=("funky",),
        tempo="88-110 bpm.",
        feel="4/4 in sixteenths, the first beat of the bar stressed hard.",
        harmony="One or two chords for a long stretch: sevenths and ninths, Dorian or "
        "Mixolydian.",
        progressions=("i7 (one chord)", "i7 IV7", "I7 IV7"),
        drums={"kick": "x.....x..x......", "snare": _BACK, "hat": "xxxxxxxxxxxxxxxx",
               "openhat": "..............x."},
        drums_note="Soft extra snares on the sixteenths around the main ones (volume "
        "30-50).",
        bass="Syncopated sixteenths with short notes and rests, landing on the first beat.",
        sounds="`slap_bass` or `bass_guitar`, a muted `guitar` playing short sixteenth "
        "chords, `electric_piano` or `organ`, `brass` stabs.",
        form="A groove of 2 or 4 bars repeated; sections change by adding or dropping "
        "parts, with a break of drums and bass alone.",
        production="Tight and dry: short notes, little reverb.",
        sources=("en.wikipedia.org/wiki/Funk (feel, harmony, bass, sounds)",),
        unsourced="the tempo (a low-authority snippet), the kick placement, the form",
    ),
    Style(
        name="disco", also=("nu disco",),
        tempo="110-130 bpm.",
        feel="4/4, the kick on every beat, sixteenths on top.",
        harmony="Major and minor sevenths and ninths, one or two chords a bar.",
        progressions=("i iv VII III", "ii V I vi", "I vi ii V"),
        drums={"kick": _FOUR, "snare": _BACK, "hat": _FOUR, "openhat": _OFF},
        bass="Octaves: the root low, then high, in eighths, with syncopation.",
        sounds="`strings` and `brass` lines, a muted `guitar` in sixteenths, "
        "`electric_piano`, `bass_guitar`.",
        form="Intro 8, verse 16, chorus 8, verse 16, chorus 8, break 8-16 (drums and "
        "bass), chorus 16.",
        production="Many doubled parts for a big sound. Reverb on strings.",
        sources=(
            "en.wikipedia.org/wiki/Disco (feel, harmony, bass, sounds, form)",
            "musicradar.com/how-to/how-to-program-a-typical-disco-drum-beat (drums)",
            "splice.com/blog/how-to-make-disco-music (tempo, harmony)",
        ),
        unsourced="the progressions, the bar counts",
    ),
    Style(
        name="reggae", also=("roots reggae", "one drop", "dub"),
        tempo="60-90 bpm.",
        feel="4/4, the strong accent on beat 3. Hats shuffled.",
        harmony="Few chords, major or minor, each a bar or two.",
        progressions=("I IV", "i VII", "I V vi IV"),
        drums={"kick": "........x.......", "rim": "........x.......", "hat": _GRID},
        drums_notes="hat:2/3 hat:1/3 hat:2/3 hat:1/3 [kick rim hat]:2/3 hat:1/3 hat:2/3 hat:1/3",
        drums_note="The one drop: nothing on beat 1.",
        bass="Heavy and round, the melody of the piece: the chord's root and fifth, with "
        "rests, in a dotted rhythm.",
        sounds="A short chord on beats 2 and 4 from `guitar` or `piano` (the skank), an "
        "`organ` between them, `bass_guitar` with a highcut, `brass` lines.",
        form="Verse and chorus over the same two chords; a section with only drums and "
        "bass.",
        production="The bass loud and dark. Delay and reverb on single hits for the dub "
        "sound.",
        sources=(
            "en.wikipedia.org/wiki/One_drop_rhythm (drums)",
            "en.wikipedia.org/wiki/Reggae (feel, bass, sounds, production)",
            "musicradar.com/how-to/how-to-program-a-typical-one-drop-reggae-beat-and-add"
            "-fills (feel, drums)",
        ),
        unsourced="the tempo (a snippet), the progressions, the form",
    ),
    Style(
        name="reggaeton", also=("dembow", "perreo", "regueton", "urbano"),
        tempo="85-100 bpm, usually 90-95.",
        feel="4/4. Over a kick on every beat, the snare plays 3+3+2: the dembow.",
        harmony="Minor, three or four chords, one a bar.",
        progressions=("i VI III VII", "VI i VII VII", "i iv VI V"),
        drums={"kick": _FOUR, "snare": "...x..x....x..x.", "shaker": "xxxxxxxxxxxxxxxx"},
        drums_note="The shaker at volume 50. A `clap` doubles some snares.",
        bass="A sine sub on the chord's root, following the kick.",
        sounds="The kit with `snare_electro` and `rim`, the `sub_bass` synth, a `pluck` or "
        "`nylon_guitar` or `piano` riff of a few notes, a `bell` lead.",
        form="8-bar sections: intro, chorus, verse, pre-chorus, chorus, bridge, outro, "
        "with the riff as the hook.",
        production="Short, dry drum sounds. The drum sounds change in bars 3-4 of a 4-bar "
        "cycle.",
        sources=(
            "musicradar.com/news/beat-building-how-to-produce-a-reggaeton-beat (tempo, "
            "drums, production)",
            "native-instruments.com/blogs/blog/reggaeton (tempo, the second progression, "
            "bass, form)",
            "en.wikipedia.org/wiki/Dembow (the 3+3+2 cell)",
        ),
        unsourced="the first and third progressions",
    ),
    Style(
        name="cumbia", also=("cumbia sonidera", "cumbia colombiana", "cumbia mexicana"),
        tempo="85-110 bpm.",
        feel="Counted in two (2/4 or cut time); written here in 4/4 with a steady scraper.",
        harmony="Simple: two or three chords, major or minor, one or two bars each.",
        progressions=("i V", "I IV V I", "i iv V i"),
        drums={"kick": "x.......x.......", "shaker": "x.xxx.xxx.xxx.xx", "clave": _OFF,
               "tom": "......x.......x."},
        drums_note="The shaker is the scraper's 'chu-chucu-chu'; the clave stands for "
        "the small drum on the off-beats.",
        bass="Root on beat 1 held two beats, then the fifth or third on beats 3 and 4.",
        sounds="`accordion` or a synth lead for the melody, `brass` answers, `bass_guitar`, "
        "`pan_flute` or `flute` for the gaita.",
        form="Intro 8 with the melody, verse 16, chorus 8, an instrumental 8 where the "
        "accordion answers, repeat.",
        production="Dry and danceable; the scraper and the bass up front.",
        sources=("en.wikipedia.org/wiki/Cumbia (the meter, the scraper, the instruments)",),
        unsourced="the tempo (snippets), the drum grid, the bass, the progressions, the "
        "form: this is the least sourced guide",
    ),
    Style(
        name="bossa nova", also=("bossa", "bossanova"),
        tempo="110-140 bpm in 4/4, soft.",
        feel="Straight, not swung. Quiet and even.",
        harmony="Rich chords: sixths, sevenths and ninths. A ii V I with a chord a bar, "
        "often moving to a new key.",
        progressions=("ii V I I", "I vi ii V", "i iv VII III"),
        drums={"kick": "x.....x.x.....x.", "rim": "x.....x.....x...", "hat": _GRID},
        drums_notes="[kick rim hat]:0.5 hat:0.5 hat:0.5 [kick rim hat]:0.5 [kick hat]:0.5 "
        "hat:0.5 [rim hat]:0.5 [kick hat]:0.5 [kick hat]:0.5 hat:0.5 [rim hat]:0.5 "
        "[kick hat]:0.5 [kick hat]:0.5 [rim hat]:0.5 hat:0.5 [kick hat]:0.5",
        drums_note="The rim's cell lasts two bars; the notes above are both. Everything "
        "soft: `kick_soft`, volume 60-80.",
        bass="Root on beat 1 and the fifth on beat 3, each with a short note before it.",
        sounds="`nylon_guitar` playing the chords in the rim's rhythm, `acoustic_bass`, "
        "`flute` or a soft `piano` for the tune, `shaker`.",
        form="A 16- or 32-bar tune played through, then again with a solo.",
        production="Intimate and dry: little reverb, nothing loud.",
        sources=(
            "en.wikipedia.org/wiki/Bossa_nova (straight feel, chord types, the guitar)",
            "premierguitar.com/rhythm-rules-brazilian-guitar-101 (ii V I, the two-bar cell)",
        ),
        unsourced="the tempo, the drum cell as written, the bass, the form",
    ),
    Style(
        name="jazz", also=("swing", "jazz swing", "bebop"),
        tempo="Medium swing 120-160 bpm; a ballad 60-90.",
        feel="4/4 swung: an eighth pair is long-short, about two to one (`2/3` then `1/3` "
        "of a beat).",
        harmony="Seventh chords. ii V I is the unit, in major (ii7 V7 Imaj7) and minor. "
        "Chords change every bar or twice a bar.",
        progressions=("ii V I I", "I vi ii V", "ii V I VI (back to ii)"),
        drums={},
        drums_notes="ride:1 ride:2/3 ride:1/3 ride:1 ride:2/3 ride:1/3 >0 r:1 pedalhat:1 r:1 "
        "pedalhat:1",
        drums_note="The ride plays 'ting, ting-ti'; the hi-hat closes on beats 2 and 4. "
        "Kick and snare only as soft accents.",
        bass="Walking: a quarter note on every beat, moving by step through the chord's "
        "notes, the root on beat 1.",
        sounds="`acoustic_bass`, `piano` playing short chords off the beat, `ride`, and a "
        "horn for the tune: `trumpet`, `tenor_sax` or `alto_sax`.",
        form="A 32-bar tune in four 8-bar parts (A A B A) or a 12-bar blues: the tune, "
        "solos over the same chords, the tune again.",
        production="Acoustic and dry: a little room reverb, no effects.",
        sources=(
            "en.wikipedia.org/wiki/Swing_(jazz_performance_style) (feel, drums, sounds)",
            "en.wikipedia.org/wiki/Ii-V-I_progression (harmony)",
            "en.wikipedia.org/wiki/Walking_bass (bass)",
        ),
        unsourced="the tempo (snippets), the 32-bar form's bar counts",
    ),
    Style(
        name="blues", also=("12 bar blues", "blues rock"),
        tempo="70-120 bpm.",
        feel="4/4 as a shuffle: an eighth pair is long-short (`2/3` then `1/3` of a beat).",
        harmony="Twelve bars, one chord a bar, all seventh chords: I I I I, IV IV I I, V IV "
        "I V. The tune uses the blues scale (in A: A C D D# E G).",
        progressions=("I I I I IV IV I I V IV I V", "I IV I I IV IV I I V IV I I",
                      "i i i i iv iv i i VI V i i"),
        drums={},
        drums_notes="[kick hat]:2/3 hat:1/3 [snare hat]:2/3 hat:1/3 [kick hat]:2/3 hat:1/3 "
        "[snare hat]:2/3 hat:1/3",
        bass="A walking figure up the chord: root, third, fifth, sixth, in shuffled "
        "eighths or quarters.",
        sounds="`electric_guitar` or `overdrive_guitar` for the tune, `piano` or `organ`, "
        "`bass_guitar`, `harmonica`.",
        form="The twelve bars repeated: a tune, solos, the tune. The last bar turns back "
        "to the start on V.",
        production="Raw and close: a little drive on the guitar, some room reverb.",
        sources=("en.wikipedia.org/wiki/Twelve-bar_blues (the three progressions, the "
                 "form, the bass figure)",),
        unsourced="the tempo, the shuffle and the drums, the sounds",
    ),
    Style(
        name="cinematic orchestral",
        also=("orchestral", "orquestal", "epic", "epica", "film score", "soundtrack",
              "trailer", "cinematic", "orquesta"),
        tempo="60-90 bpm for slow and wide; 110-140 for action.",
        feel="4/4 or 3/4, or 6/8 for a rolling pulse. Loudness and density change through "
        "the piece.",
        harmony="Minor, simple chords held long, the bass moving under them. A repeated "
        "short figure in the strings drives it.",
        progressions=("i VI III VII", "i VII VI V", "i iv VI V"),
        drums={},
        drums_note="No kit: `timpani` on the strong beats, low `tom` hits, a `crash` at a "
        "peak. Written as their own pitched or drum tracks.",
        bass="`contrabass` and `cello` on the chord's root in long notes; `tuba` for "
        "weight.",
        sounds="Each instrument in its own range (action check names a note outside it): "
        "`violin` high, `viola`, `cello`, `contrabass`; `french_horn` for the tune, "
        "`trombone` and `trumpet` for power; `flute`, `oboe`, `clarinet`; `harp`, "
        "`timpani`, `choir`. `strings` and `brass` are whole sections in one track.",
        form="A slow start with few instruments (8-16 bars), a second part that adds the "
        "string figure and low brass (16), a peak with everything (8-16), a short quiet "
        "end.",
        production="Long reverb on the whole song (a hall). `automation` of each track's "
        "volume for swells: quiet to loud over 2-4 bars. Spread the instruments left and "
        "right with `pan`.",
        sources=(),
        unsourced="all of it: only search snippets were found (string figures, heavy "
        "brass and percussion, a trailer's three parts). The ranges are in action check.",
    ),
    Style(
        name="chiptune", also=("8-bit", "8 bit", "chip", "videogame", "game boy", "nes"),
        tempo="120-160 bpm.",
        feel="4/4, straight and exact.",
        harmony="Major and bright, or minor for tension. A chord is not held: its notes "
        "are played one after another very fast, since the old chips had few voices.",
        progressions=("I V vi IV", "I IV V I", "i VII VI VII"),
        drums={"kick": "x.......x.......", "snare": _BACK, "hat": _GRID},
        drums_note="Better than the kit: a noise track. A `sound` with one noise "
        "oscillator, no sustain and a short decay, played short for the hat and longer "
        "for the snare.",
        bass="A triangle wave on the chord's root in eighths or octaves, C1-C3.",
        sounds="Lead and arpeggio: a square wave (`sound` with one `square` oscillator, "
        "or the presets of the SID and Nescaline synths; action sounds lists them). Bass: "
        "one `triangle` oscillator. At most four parts sound at once.",
        form="A 16-bar loop in 4-bar phrases, then a second 16-bar part, and around again.",
        production="No reverb, no effects: the plain waves are the style. `bitcrush` "
        "makes other sounds fit.",
        sources=(
            "en.wikipedia.org/wiki/Chiptune (the arpeggio, the waves, production)",
            "blog.landr.com/how-to-make-chiptune-music (harmony, drums, form)",
        ),
        unsourced="the tempo, the drum grid, the progressions",
    ),
)


def find(query: str) -> Style | None:
    """The style a name means, by its name or one of its other names; else the one whose
    name it holds or is closest to."""
    wanted = _fold(query)
    if not wanted:
        return None
    names = {_fold(name): style for style in STYLES for name in (style.name, *style.also)}
    if wanted in names:
        return names[wanted]
    inside = [style for name, style in names.items() if name in wanted or wanted in name]
    if inside:
        return inside[0]
    close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.75)
    return names[close[0]] if close else None


def listing() -> str:
    """The genres there is a guide for."""
    return ", ".join(style.name for style in STYLES)


def text(style: Style) -> str:
    """A guide as the model reads it."""
    lines = [
        f"Style: {style.name}. Conventions of the genre, not rules: keep them until the "
        "piece works, then bend them.",
        f"Tempo: {style.tempo}",
        f"Feel: {style.feel}",
        f"Harmony: {style.harmony}",
        "Chord loops (degrees of the key; upper case major, lower case minor): "
        + "; ".join(style.progressions),
    ]
    if style.drums:
        steps = len(next(iter(style.drums.values())))
        lines.append("Drums, one bar, 4 steps a beat (x a hit):")
        lines += [
            f"  {name:8}{_spaced(style.drums[name])}" for name in _ROWS if name in style.drums
        ]
        if not style.drums_notes:
            lines.append(f"  as the notes of a `drums` track: {notes(style.drums)}")
        if steps != 16:
            lines.append(f"  ({steps} steps: the bar is {steps // 4} beats long.)")
    if style.drums_notes:
        lines.append(f"  as the notes of a `drums` track, with the feel: {style.drums_notes}")
    if style.drums_note:
        lines.append(f"  {style.drums_note}")
    lines += [
        f"Bass: {style.bass}",
        f"Sounds, with this tool: {style.sounds}",
        f"Form: {style.form}",
        f"Production: {style.production}",
        "Write a new piece in this style: its own chords within these, its own tune.",
    ]
    return "\n".join(lines)


def notes(drums: Mapping[str, str]) -> str:
    """A drum grid as the notes of a `drums` track: each hit lasts until the next, so
    the bar adds up to its beats."""
    steps = len(next(iter(drums.values())))
    hits = [
        (step, [name for name in _ROWS if name in drums and drums[name][step] != "."])
        for step in range(steps)
    ]
    hits = [(step, names) for step, names in hits if names]
    tokens = [f"r:{hits[0][0] / 4:g}"] if hits and hits[0][0] else []
    for index, (step, names) in enumerate(hits):
        until = hits[index + 1][0] if index + 1 < len(hits) else steps
        head = names[0] if len(names) == 1 else "[" + " ".join(names) + "]"
        tokens.append(f"{head}:{(until - step) / 4:g}")
    return " ".join(tokens)


def _spaced(row: str) -> str:
    return " ".join(row[index : index + 4] for index in range(0, len(row), 4))


def _fold(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())
