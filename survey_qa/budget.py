import json

"""Hard spend guard: every model call is estimated first and refused if it could cross the run's cap."""

# Measured 2026-10-26 on muse-spark-1.3: 3 images + prompt ~= 10.0K input tokens, ~1.2K output.
TOKENS_PER_IMAGE = 3300
# Sent as max_output_tokens on every request, and reserved in full before the call, so one long
# reasoning reply can never push the run past its cap. 2000 was too low: 2 of 5 smoke calls spent
# all of it reasoning and returned no text (2026-10-27).
MAX_OUTPUT_TOKENS = 4000
# chars//3 and 3300/image are guesses, not bounds: pad the input estimate so one underestimate
# cannot carry the last allowed call far past the cap. Measured 10-27: raw ~7K vs real 6.15K, so 1.2.
INPUT_SAFETY = 1.2


class BudgetExceeded(RuntimeError):
    pass


class UsageMissing(BudgetExceeded):
    """A real call came back without token counts: the guard would be blind, so the run stops."""


class Budget:
    def __init__(self, max_tokens, max_usd=None, usd_per_m_in=None, usd_per_m_out=None, ledger=None):
        # Dollar cap needs a price. Price for muse-spark-1.3 is still unknown, so a dollar cap
        # without prices is refused rather than silently ignored.
        if max_usd is not None and (usd_per_m_in is None or usd_per_m_out is None):
            raise ValueError("max_usd set but no per-token price given; pass --usd-in/--usd-out")
        self.max_tokens, self.max_usd = max_tokens, max_usd
        self.p_in, self.p_out = usd_per_m_in or 0.0, usd_per_m_out or 0.0
        self.used_in = self.used_out = 0
        self.calls = self.estimated_calls = 0
        self.stopped = None  # latched: once a stop happens, every later check refuses
        # Ledger: a reservation line is flushed to disk BEFORE each request, so a kill or Ctrl+C
        # mid-request still leaves the worst-case charge on record.
        self.ledger = open(ledger, "a", encoding="utf-8") if ledger else None
        self.last_estimate = {"input_tokens": 0, "output_tokens": 0}

    def usd(self, tin, tout):
        return tin * self.p_in / 1e6 + tout * self.p_out / 1e6

    @property
    def spent_usd(self):
        return self.usd(self.used_in, self.used_out)

    def estimate(self, n_images, text_chars):
        return int((n_images * TOKENS_PER_IMAGE + text_chars // 3) * INPUT_SAFETY), MAX_OUTPUT_TOKENS

    def check(self, n_images, text_chars):
        if self.stopped:
            raise BudgetExceeded(f"run already stopped: {self.stopped}")
        tin, tout = self.estimate(n_images, text_chars)
        self.last_estimate = {"input_tokens": tin, "output_tokens": tout}
        if self.used_in + self.used_out + tin + tout > self.max_tokens:
            raise BudgetExceeded(f"token cap {self.max_tokens}: used {self.used_in + self.used_out}, "
                                 f"next call est {tin + tout}")
        if self.max_usd is not None and self.spent_usd + self.usd(tin, tout) > self.max_usd:
            raise BudgetExceeded(f"dollar cap ${self.max_usd}: spent ${self.spent_usd:.4f}")
        self._log("reserve", self.last_estimate)

    def _log(self, kind, usage):
        if self.ledger:
            self.ledger.write(json.dumps({"kind": kind, **usage}) + "\n")
            self.ledger.flush()

    def stop(self, why):
        self.stopped = why
        return BudgetExceeded(why)

    def record(self, usage):
        self.calls += 1
        self.used_in += int(usage.get("input_tokens", 0))
        self.used_out += int(usage.get("output_tokens", 0))

    def record_real(self, usage):
        """Record a billed call. Missing or zero counts are charged at the pre-call estimate, then the run stops."""
        ok = isinstance(usage, dict) and all(
            type(usage.get(k)) is int and usage[k] > 0 for k in ("input_tokens", "output_tokens"))
        if not ok:
            self.record_failed()
            raise UsageMissing(f"call returned no usable token counts {usage!r}; charged estimate and stopped")
        self.record(usage)
        self._log("actual", {k: usage[k] for k in ("input_tokens", "output_tokens")})

    def record_failed(self):
        """A request that errored or timed out may still be billed: charge the estimate."""
        self.record(self.last_estimate)
        self.estimated_calls += 1
        self._log("estimated", self.last_estimate)

    def summary(self):
        return {"calls": self.calls, "estimated_calls": self.estimated_calls, "input_tokens": self.used_in, "output_tokens": self.used_out,
                "usd": round(self.spent_usd, 4) if self.max_usd is not None or self.p_in else None,
                "max_tokens": self.max_tokens, "max_usd": self.max_usd}
