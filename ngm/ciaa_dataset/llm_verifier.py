"""LLM-based verification for defendant name matching in gray-zone cases.

Supports Google Gemini models for verification.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Retry config for transient errors (503, rate limits)
_MAX_RETRIES = 3
_RETRY_DELAYS = [5, 15, 30]  # seconds between retries


class LLMVerifier:
    """Verifies defendant name matches using LLM for gray-zone cases."""

    def __init__(self):
        """
        Initialize LLM verifier.

        Supports Google Gemini: "google_genai:model-name" (e.g., "google_genai:gemini-2.0-flash-lite")
        """
        self._client = None

        # Get LLM configuration
        self.api_key = os.environ.get("LLM_API_KEY")
        llm_config = os.environ.get("LLM")

        if not self.api_key:
            raise ValueError("LLM_API_KEY environment variable not set")

        if not llm_config:
            raise ValueError(
                "LLM environment variable not set (expected format: google_genai:model-name)"
            )

        # Parse model from config
        if ":" in llm_config:
            provider, model = llm_config.split(":", 1)
            if provider.strip() != "google_genai":
                raise ValueError(
                    f"Unsupported provider: {provider}. Only google_genai is supported"
                )
            self.model = model.strip()
        else:
            raise ValueError(
                f"Invalid model format: {llm_config}. Expected format: google_genai:model-name"
            )

        if not self.model:
            raise ValueError("Model name must not be empty")

    def _get_client(self):
        """Lazy load Google Gemini client."""
        if self._client is not None:
            return self._client

        try:
            from google import genai

            client = genai.Client(api_key=self.api_key)
            self._client = client
            logger.info("Initialized Google Gemini client with model: %s", self.model)
        except ImportError as e:
            raise ImportError(
                "google-genai package not installed. Run: pip install google-genai"
            ) from e

        return self._client

    def _verify_single_batch(
        self, cases: list[dict]
    ) -> dict[str, tuple[Optional[int], float, str]]:
        """
        Verify a single batch of cases with LLM.

        Internal method used by verify_multi_case_batch for chunked processing.
        """
        # Build prompt for this batch
        cases_text = []
        for i, case in enumerate(cases):
            case_num = case["case_number"]
            defendants = case["defendant_names"]
            candidates = case["press_release_candidates"]

            # Format defendants as numbered list
            defendants_text = "\n   ".join(
                [f"{j+1}. {name}" for j, name in enumerate(defendants)]
            )

            candidates_text = "\n   ".join(
                [
                    f"{j+1}. PR {pr.get('press_id')}: {pr.get('title', '')} {((pr.get('full_text') or '').strip())[:800]}"
                    for j, pr in enumerate(candidates)
                ]
            )

            cases_text.append(
                f"""Case {i+1}: {case_num}
   Defendants:
   {defendants_text}
   Press Release Candidates:
   {candidates_text}"""
            )

        all_cases_text = "\n\n".join(cases_text)

        prompt = f"""You are verifying which CIAA press release (if any) genuinely matches defendants from multiple court cases.

{all_cases_text}

For each case, decide if ANY press release is genuinely about the same case as the defendants listed.

RULES:
- A name match alone is NOT enough. The press release must be about the SAME corruption case.
- Require meaningful context overlap: same location, same institution, same type of crime.
- If the PR is about an annual report, interaction program, or unrelated event — answer null.
- Different surnames = different person. "भिम प्रसाद लोवा" ≠ "भीम प्रसाद आचार्य" (different surname).
- A single surname match like "कार्की" or "यादव" is NOT sufficient — require full name match.
- If unsure, prefer null over a wrong match.
- NEPALI SPELLING VARIATIONS ARE VALID MATCHES: ण/न (मण्डल=मंडल), व/ब, ष/श, ं/ँ are the same character written differently. "उपेन्द्र मण्डल" and "उपेन्द्र मंडल" ARE the same person.

Answer with JSON only (no other text):
{{
  "results": [
    {{
      "case_number": "080-CR-XXXX",
      "matched_pr_index": null or 1-N (the number from the candidate list for that case),
      "confidence": 0.0 to 1.0,
      "explanation": "brief explanation in English — state what specific text in the PR confirms the match"
    }},
    ...
  ]
}}"""

        try:
            client = self._get_client()

            response_text = None
            last_error = None

            for attempt in range(_MAX_RETRIES):
                try:
                    response = client.models.generate_content(
                        model=self.model, contents=prompt, config={"temperature": 0.0}
                    )
                    response_text = response.text
                    break  # success
                except Exception as e:
                    last_error = e
                    err_str = str(e)
                    is_transient = (
                        "503" in err_str
                        or "UNAVAILABLE" in err_str
                        or "429" in err_str
                        or "rate" in err_str.lower()
                    )

                    if is_transient and attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_DELAYS[attempt]
                        logger.warning(
                            "LLM transient error (attempt %d/%d), retrying in %ds: %s",
                            attempt + 1,
                            _MAX_RETRIES,
                            delay,
                            err_str[:100],
                        )
                        time.sleep(delay)
                    else:
                        raise

            if not response_text:
                raise last_error or Exception("Failed to get response from LLM")

            # Parse JSON response
            response_data = json.loads(response_text)
            results_list = response_data.get("results", [])

            # Build results dict
            results = {}
            for i, case in enumerate(cases):
                case_num = case["case_number"]

                # Find matching result
                result = None
                for r in results_list:
                    if r.get("case_number") == case_num:
                        result = r
                        break

                if result:
                    # Safely parse matched_pr_index (LLM might return string)
                    matched_index_raw = result.get("matched_pr_index")
                    try:
                        matched_index = (
                            int(matched_index_raw)
                            if matched_index_raw is not None
                            else None
                        )
                    except (TypeError, ValueError):
                        matched_index = None

                    # Safely parse confidence (LLM might return non-numeric)
                    confidence_raw = result.get("confidence")
                    try:
                        confidence = (
                            float(confidence_raw) if confidence_raw is not None else 0.0
                        )
                    except (TypeError, ValueError):
                        confidence = 0.0

                    explanation = result.get("explanation", "")

                    # Convert 1-based index to press_id
                    matched_press_id = None
                    candidates = case["press_release_candidates"]
                    if matched_index is not None and 1 <= matched_index <= len(
                        candidates
                    ):
                        raw_press_id = candidates[matched_index - 1].get("press_id")
                        try:
                            matched_press_id = (
                                int(raw_press_id) if raw_press_id is not None else None
                            )
                        except (TypeError, ValueError):
                            matched_press_id = None
                            logger.warning(
                                "[%s] Failed to parse press_id from candidate %d: %s",
                                case_num,
                                matched_index,
                                raw_press_id,
                            )

                    results[case_num] = (matched_press_id, confidence, explanation)

                    logger.debug(
                        "[%s] LLM batch: matched_press_id=%s, confidence=%.2f",
                        case_num,
                        matched_press_id,
                        confidence,
                    )
                else:
                    # No result found for this case
                    results[case_num] = (None, 0.0, "No result from LLM")
                    logger.warning("[%s] No result in LLM batch response", case_num)

            return results

        except json.JSONDecodeError as e:
            logger.error("Failed to parse LLM batch response: %s", e)
            return {
                case["case_number"]: (None, 0.0, f"llm_error: JSON parse error: {e}")
                for case in cases
            }
        except Exception as e:
            logger.error("LLM batch verification failed: %s", e)
            return {
                case["case_number"]: (None, 0.0, f"llm_error: {e}") for case in cases
            }

    def verify_multi_case_batch(
        self,
        cases: list[dict],
        chunk_size: int = 10,
    ) -> dict[str, tuple[Optional[int], float, str]]:
        """
        Verify multiple cases using chunked LLM calls for resilience.

        Processes cases in batches to avoid context/rate limit failures.
        If one batch fails, other batches continue processing.

        Args:
            cases: List of dicts with keys:
                - case_number: str
                - defendant_names: list[str]
                - press_release_candidates: list[dict] with 'press_id' and 'title'
            chunk_size: Maximum cases per LLM call (default: 10)

        Returns:
            Dict mapping case_number -> (matched_press_id or None, confidence, explanation)
        """
        if not cases:
            return {}

        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0")

        all_results: dict[str, tuple[Optional[int], float, str]] = {}

        # Process in chunks to avoid context/rate limit failures
        total_cases = len(cases)
        for i in range(0, total_cases, chunk_size):
            batch = cases[i : i + chunk_size]
            batch_num = (i // chunk_size) + 1
            total_batches = (total_cases + chunk_size - 1) // chunk_size

            logger.info(
                "Processing LLM batch %d/%d (%d cases)",
                batch_num,
                total_batches,
                len(batch),
            )

            try:
                batch_results = self._verify_single_batch(batch)
                all_results.update(batch_results)
            except Exception as e:
                # Log error but continue with other batches
                logger.error("Batch %d/%d failed: %s", batch_num, total_batches, e)
                # Add failure results for this batch — mark as error not rejection
                for case in batch:
                    all_results[case["case_number"]] = (
                        None,
                        0.0,
                        f"llm_error: {e}",
                    )

        return all_results
