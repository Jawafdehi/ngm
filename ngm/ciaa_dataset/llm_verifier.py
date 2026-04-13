"""LLM-based verification for defendant name matching in gray-zone cases."""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class LLMVerifier:
    """Verifies defendant name matches using LLM for gray-zone cases."""

    def __init__(self):
        """
        Initialize LLM verifier.

        Uses LLM_API_KEY and LLM env variables to determine provider and model.
        LLM format: "google_genai:model-name" or just "model-name"
        """
        self._client = None
        llm_config = os.environ.get("LLM")

        if not llm_config:
            raise ValueError(
                "LLM environment variable not set (expected format: provider:model or just model-name)"
            )

        # Parse provider:model format
        if ":" in llm_config:
            provider, model = llm_config.split(":", 1)
            self.provider = provider.strip()
            self.model = model.strip()
        else:
            self.provider = "google_genai"
            self.model = llm_config.strip()

        if not self.model:
            raise ValueError("LLM model name must not be empty")

        if self.provider != "google_genai":
            raise ValueError(
                f"Unsupported LLM provider: {self.provider}. Supported: google_genai"
            )

        self.api_key = os.environ.get("LLM_API_KEY")

        if not self.api_key:
            raise ValueError("LLM_API_KEY environment variable not set")

    def _get_client(self):
        """Lazy load LLM client."""
        if self._client is not None:
            return self._client

        try:
            from google import genai

            client = genai.Client(api_key=self.api_key)
            self._client = client
            logger.info("Initialized Gemini client with model: %s", self.model)
        except ImportError:
            raise ImportError(
                "google-genai package not installed. Run: pip install google-genai"
            )

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

        prompt = f"""You are verifying which CIAA press release (if any) matches defendants from multiple court cases.

{all_cases_text}

For each case, determine which press release (if any) mentions ANY of the defendants. Consider:
- Nepali spelling variations (व/ब, ं/ँ, ष/श, ङ्ग/ंग, etc.)
- Name order differences (first name last vs last name first)
- Partial name matches (first name only, last name only)
- Common honorifics that may be present or absent

Answer with JSON only (no other text):
{{
  "results": [
    {{
      "case_number": "080-CR-XXXX",
      "matched_pr_index": null or 1-N (the number from the candidate list for that case),
      "confidence": 0.0 to 1.0,
      "explanation": "brief explanation in English"
    }},
    ...
  ]
}}"""

        try:
            client = self._get_client()

            # Generate response using Gemini
            response = client.models.generate_content(model=self.model, contents=prompt)
            response_text = response.text

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
                case["case_number"]: (None, 0.0, f"JSON parse error: {e}")
                for case in cases
            }
        except Exception as e:
            logger.error("LLM batch verification failed: %s", e)
            return {
                case["case_number"]: (None, 0.0, f"LLM error: {e}") for case in cases
            }

    def verify_multi_case_batch(
        self,
        cases: list[dict],
        chunk_size: int = 20,
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
            chunk_size: Maximum cases per LLM call (default: 20)

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
                # Add failure results for this batch
                for case in batch:
                    all_results[case["case_number"]] = (
                        None,
                        0.0,
                        f"Batch processing error: {e}",
                    )

        return all_results
