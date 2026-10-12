# HumanTyping timing integration

Source: [Lax3n/HumanTyping](https://github.com/Lax3n/HumanTyping/tree/db142362206f46b9a9d0c2687c88ab4105b9b9d1), version 1.0.3, revision `db142362206f46b9a9d0c2687c88ab4105b9b9d1`.
Copyright (c) 2025 HumanTyping Contributors. The original MIT license is in `LICENSE`.

`language.py` is copied unchanged. `timing.py` adapts the timing calculation,
speed/fatigue parameters and QWERTY distance lookup from `typer.py`, `config.py`
and `keyboard.py`. It preserves common-word and bigram acceleration, complex-word
and distant-key penalties, uppercase/space pauses and capped fatigue.

Retail Desk adaptations:

- Use the controller's `random.Random` instance instead of global NumPy state.
- Yield timings without generating errors, corrections or a plaintext history.
- Cache each word's classification while traversing a field.
- Apply the timing formula to every supported ASCII character. Unicode and
  specialized inputs continue to use the application's native fill fallback.
- Bound each modeled interval to 65–250 ms, including the existing key hold.
  Browser calls add overhead; these values do not guarantee measured WPM.
- The application scales these intervals with its Slow, Balanced, Fast and
  Aggressive pacing profiles, which also control pointer travel and click dwell.
- Keep browser ownership, actionability, key chords, exact-value verification,
  cancellation and operation deadlines in `retail/behavior.py`.

The upstream browser adapters, error simulator, demo and NumPy/Selenium/Appium
dependencies are not included. No runtime download or additional installation is
needed. This is an adapted timing subset, not the complete HumanTyper API.
