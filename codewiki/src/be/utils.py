import logging
from typing import Any

import tiktoken

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# ---------------------- Complexity Check --------------------
# ------------------------------------------------------------


def is_complex_module(components: dict[str, Any], core_component_ids: list[str]) -> bool:
    files = set()
    for component_id in core_component_ids:
        if component_id in components:
            files.add(components[component_id].file_path)

    result = len(files) > 1

    return result


# ------------------------------------------------------------
# ---------------------- Token Counting ---------------------
# ------------------------------------------------------------

enc = tiktoken.encoding_for_model("gpt-4")


def count_tokens(text: str) -> int:
    """
    Count the number of tokens in a text.
    """
    # source code may contain literal special tokens such as "<|endoftext|>"
    # (e.g. CLIP tokenizers); count them as plain text instead of raising
    length = len(enc.encode(text, disallowed_special=()))
    # logger.debug(f"Number of tokens: {length}")
    return length
