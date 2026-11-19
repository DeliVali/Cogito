---
name: compose-music
description: Use when the person asks for music with LMMS — to compose a song, a beat, a tune, a melody, a soundscape or a remix, in any genre (pop, rock, jazz, blues, funk, disco, house, techno, trance, trap, lofi, synthwave, ambient, dubstep, reggae, orchestral, chiptune) or like a reference song. Música con LMMS (compón o componer una canción, un beat, un ritmo, una melodía, una rola, un paisaje sonoro, música en un género o estilo como reggaeton, cumbia, bossa nova, salsa, orquestal, electrónica, con sintetizadores o batería).
---

# Composing music with `lmms`

You cannot hear. You write notes and settings; the person listens and tells you what to
change. So work in a way that keeps what you write correct, and ask the person early.

## 1. Know the style before any note

- The person named a genre: call `lmms` with action `style` and the genre. It returns
  the tempo, the feel, the chords, the drum pattern, the bass, the sounds, the form and
  the production of that genre, written for this tool. Follow it; do not write a genre
  from memory when a guide exists.
- No guide for it: search the web for how that genre is made (tempo, drum pattern,
  chords, instruments) before you write.
- The person gave a recording as reference: call `study_song` on it. Take its tempo,
  its feel, the kind of chord loop and the groove. Never its tune: the piece must be new.
- The request is vague ("something chill"): pick a style, say which in one line, and go.

## 2. Choose the sounds

Call `lmms` with action `sounds` once, to see how a `sound`, a `sample`, `effects` and
`automation` are written and which presets exist. Then decide each track's sound before
writing notes: a preset by name, a designed synth, a General MIDI instrument, a sample.

## 3. Write it in sections, not all at once

- Decide the key, the tempo, the chord loop and the form first, and say them in one
  line each.
- Write one section first (8 or 16 bars): drums, bass, chords, then the lead.
- Count the beats of every bar. Each track's notes for a 4/4 bar add up to 4. A rest
  is `r:BEATS`. A miscounted bar moves everything after it.
- Nothing repeats by itself: a pattern that should last the song is written for every
  bar of it. Write it once, then repeat that text.
- Keep the bass on the chord's root at the start of each bar unless the style says
  otherwise, and the lead inside the key.

## 4. Check before the person listens

After every write, call `lmms` with action `check`. It reads your notes back as key,
chords and drum pattern and lists what looks wrong. Fix what it finds, check again.
With a reference recording, pass it as `reference`: if the result says the piece is too
close, change the key or the chord loop.

## 5. Render, and read what it says

Call action `render`. Its result says whether the mix is silent, quiet, or clipping.
Fix that with the tracks' `volume` before anything else, and render again.

## 6. Hand it over

Open it for the person (action `open`) or tell them where the audio file is. Say in a
few lines what you made: style, tempo, key, sections, sounds. Then ask what to change.
When they answer, read the project first (action `read`): they may have edited it in
LMMS. Change only what they asked for; a track you do not name stays as it is.
