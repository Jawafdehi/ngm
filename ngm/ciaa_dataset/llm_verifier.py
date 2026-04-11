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

        # Parse provider:model format
        if ":" in llm_config:
            provider, model = llm_config.split(":", 1)
            self.provider = provider
            self.model = model
        else:
            self.provider = "google_genai"
            self.model = llm_config

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

    def verify_multi_case_batch(
        self,
        cases: list[dict],
    ) -> dict[str, tuple[Optional[int], float, str]]:
        """
        Verify multiple cases in a single LLM call for maximum efficiency.

        Args:
            cases: List of dicts with keys:
                - case_number: str
                - defendant_names: list[str]
                - press_release_candidates: list[dict] with 'press_id' and 'title'

        Returns:
            Dict mapping case_number -> (matched_press_id or None, confidence, explanation)
        """
        if not cases:
            return {}

        # Build prompt for all cases
        cases_text = []
        for i, case in enumerate(cases):
            case_num = case["case_number"]
            defendants = case[
                "defendant_names"
            ]  # Changed from defendant_name to defendant_names (list)
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
                    matched_index = result.get("matched_pr_index")
                    confidence = float(result.get("confidence", 0.0))
                    explanation = result.get("explanation", "")

                    # Convert 1-based index to press_id
                    matched_press_id = None
                    candidates = case["press_release_candidates"]
                    if matched_index is not None and 1 <= matched_index <= len(
                        candidates
                    ):
                        matched_press_id = int(
                            candidates[matched_index - 1].get("press_id", 0)
                        )

                    results[case_num] = (matched_press_id, confidence, explanation)

                    logger.debug(
                        "[%s] LLM multi-case batch: matched_press_id=%s, confidence=%.2f",
                        case_num,
                        matched_press_id,
                        confidence,
                    )
                else:
                    # No result found for this case
                    results[case_num] = (None, 0.0, "No result from LLM")
                    logger.warning(
                        "[%s] No result in LLM multi-case batch response", case_num
                    )

            return results

        except json.JSONDecodeError as e:
            logger.error("Failed to parse LLM multi-case batch response: %s", e)
            return {
                case["case_number"]: (None, 0.0, f"JSON parse error: {e}")
                for case in cases
            }
        except Exception as e:
            logger.error("LLM multi-case batch verification failed: %s", e)
            return {
                case["case_number"]: (None, 0.0, f"LLM error: {e}") for case in cases
            }
