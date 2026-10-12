"""Adapted from HumanTyping's MarkovTyper timing and QWERTY distance model.

Copyright (c) 2025 HumanTyping Contributors. MIT license in LICENSE.
Upstream: db142362206f46b9a9d0c2687c88ab4105b9b9d1.
Only timing is modeled: no typos, corrections, input history or browser control.
"""
import math

from .language import get_word_difficulty, is_common_bigram


KEY_POSITIONS = {
    char: (row, column)
    for row, keys in enumerate(('`1234567890-=', 'qwertyuiop[]\\', "asdfghjkl;'", 'zxcvbnm,./'))
    for column, char in enumerate(keys)
}

# Retail Desk bounds include both the key hold and the inter-key pause.
MIN_INTERVAL = .065
MAX_INTERVAL = .250


def _distance(first, second):
    first_pos = KEY_POSITIONS.get(first.lower())
    second_pos = KEY_POSITIONS.get(second.lower())
    if first_pos is None or second_pos is None:
        return 4.0
    return math.dist(first_pos, second_pos)


def keystroke_intervals(text, rng, *, target_wpm=70):
    """Yield bounded seconds per character using the caller's private RNG.

    A fresh generator scopes speed/fatigue to one field. The caller owns input
    values; no history or state is retained on the page's long-lived controller.
    """
    if not math.isfinite(target_wpm) or target_wpm <= 0:
        raise ValueError('target_wpm must be a positive finite number')
    if not text:
        return
    session_wpm = max(10, rng.gauss(target_wpm, 10))
    base_time = 60 / (session_wpm * 5)
    fatigue = 1.0
    previous = None
    word_end = -1
    difficulty = 'normal'
    for index, char in enumerate(text):
        # Match upstream's space-delimited word context without rescanning it
        # for every character. A space itself has no common-word boost.
        if index > word_end:
            word_end = text.find(' ', index)
            if word_end == -1:
                word_end = len(text)
            difficulty = get_word_difficulty(text[index:word_end])
        fatigue = min(1.5, fatigue * 1.0005)
        interval = base_time * fatigue
        if char != ' ':
            if difficulty == 'common':
                interval *= .6
            elif difficulty == 'complex':
                interval *= 1.3
        if previous is not None:
            if is_common_bigram(previous, char):
                interval *= .4
            else:
                distance = _distance(previous, char)
                if 0 < distance < 2:
                    interval *= .5
                elif distance > 4:
                    interval *= 1.2
        if char == ' ':
            interval += rng.gauss(.25, .05)
        elif char.isupper():
            interval += .2
        interval = max(.15 * base_time, interval)
        yield max(MIN_INTERVAL, min(MAX_INTERVAL, rng.gauss(interval, .03)))
        previous = char
