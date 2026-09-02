"""
Unit tests for libs.utils, focused on preprocess_text.
"""

from typing import Any, Dict

import pytest

from libs.utils import convert_replacement, preprocess_text


def make_config(**overrides: Any) -> Dict[str, Any]:
    """Build a minimal patcher-config-like dict for preprocess_text tests."""
    config: Dict[str, Any] = {
        "pcName": "adventurer",
        "pcRace": "person",
        "pcGender": "neutral",
        "identityTokens": {
            "CHARNAME": "pcName",
            "GABBER": "pcName",
            "PRO_RACE": "pcRace",
            "RACE": "pcRace",
        },
        "genderTokens": {},
        "phoneticRules": [],
    }
    config.update(overrides)
    return config


# ==============================================================================
# Identity Tokens
# ==============================================================================


def test_identity_tokens_replace_charname_and_gabber_with_pc_name():
    config = make_config()
    result = preprocess_text("Hi <CHARNAME>, said <GABBER>.", config)
    assert result == "Hi adventurer, said adventurer."


def test_identity_tokens_replace_race_and_pro_race_with_pc_race():
    config = make_config()
    result = preprocess_text("A <RACE> and a <PRO_RACE> walk in.", config)
    assert result == "A person and a person walk in."


def test_identity_tokens_use_config_driven_mapping_not_hardcoded_names():
    """
    identityTokens is a dict of token -> source config key, so a custom
    token name mapped to pcRace should resolve correctly even though it
    isn't one of the originally hardcoded names (CHARNAME/GABBER/PRO_RACE/RACE).
    """
    config = make_config(
        identityTokens={"HERO": "pcName", "SPECIES": "pcRace"}
    )
    result = preprocess_text("<HERO> is a <SPECIES>.", config)
    assert result == "adventurer is a person."


def test_identity_tokens_missing_source_key_leaves_token_unresolved_then_stripped():
    """If the source key referenced by identityTokens isn't in the config,
    the token is left unresolved and removed by the final cleanup stage."""
    config = make_config(identityTokens={"CHARNAME": "pcNickname"})
    result = preprocess_text("Hello <CHARNAME>!", config)
    assert result == "Hello !"


def test_identity_tokens_empty_dict_leaves_all_tokens_to_cleanup_stage():
    config = make_config(identityTokens={})
    result = preprocess_text("Hi <CHARNAME>, a <RACE>.", config)
    assert result == "Hi , a ."


def test_identity_tokens_missing_key_in_config_defaults_to_empty_dict():
    config = make_config()
    del config["identityTokens"]
    result = preprocess_text("Hi <CHARNAME>.", config)
    assert result == "Hi ."


def test_identity_tokens_respects_custom_pc_name_and_race_values():
    config = make_config(pcName="Gorion", pcRace="dwarf")
    result = preprocess_text("Talk to <CHARNAME>, the <RACE>.", config)
    assert result == "Talk to Gorion, the dwarf."


# ==============================================================================
# Gender Tokens
# ==============================================================================


def test_gender_tokens_replace_based_on_pc_gender():
    config = make_config(
        pcGender="female",
        genderTokens={"HESHE": {"male": "he", "female": "she", "neutral": "they"}},
    )
    result = preprocess_text("<HESHE> arrives.", config)
    assert result == "she arrives."


# ==============================================================================
# Phonetic Rules
# ==============================================================================


def test_phonetic_rules_apply_regex_substitution():
    config = make_config(
        phoneticRules=[
            {"pattern": r"\bUst\b", "replacement": "Oost", "comment": "pronunciation"}
        ]
    )
    result = preprocess_text("Welcome to Ust.", config)
    assert result == "Welcome to Oost."


def test_phonetic_rules_invalid_regex_is_skipped_without_crash():
    config = make_config(
        phoneticRules=[{"pattern": "[", "replacement": "X"}]
    )
    result = preprocess_text("unchanged text", config)
    assert result == "unchanged text"


# ==============================================================================
# Vocal-sound / onomatopoeia silencing phonetic rule
# ==============================================================================

VOCAL_SOUND_PATTERN = (
    r"\*(?:[Aa]chooo?|ahem|belch|boom|bu+r[pp]|cackle|chuckle|cough|crunch|er[pf]"
    r"|gag|gasp|giggle|glug|grin|groan|growl|gr+u?m+ble|gr+w+r+|gulp|gurgle|gurk"
    r"|hack|hic(?:cup(?:ping)?)?|his+s+|hum|moan|mumble|mutter|nngh|pant|retch|sigh|shiver"
    r"|shrug|shudder|snarl|snicker|sniff(?:le)?|snork|snort|snore|sob|squeak"
    r"|u+r[pp]|whinn+(?:y|ie|e)|whistle|yawn)[!\s]*\*"
)


def _vocal_sound_config(**overrides: Any) -> Dict[str, Any]:
    """Config with just the vocal-sound/onomatopoeia silencing rule."""
    return make_config(
        phoneticRules=[
            {
                "pattern": VOCAL_SOUND_PATTERN,
                "replacement": "",
                "comment": "silence vocal sounds, audio effects, and onomatopoeia within asterisks",
            },
        ],
        **overrides,
    )


@pytest.mark.parametrize(
    "text",
    [
        "*burp*",
        "*sigh*",
        "*ahem*",
        "*achoo*",
        "*achooo*",
        "*hiccup*",
        "*hic*",
        "*hiccupping*",
        "*hisss*",
        "*hiss*",
        "*whinne*",
        "*whinnne*",
        "*whinny*",
        "*whinnie*",
        "*grumble*",
        "*grrrumble*",
        "*urp*",
        "*uurp*",
        "*sniff*",
        "*sniffle*",
        "*chuckle*",
        "*cackle*",
        "*giggle*",
        "*groan*",
        "*growl*",
        "*gurgle*",
        "*gurk*",
        "*mumble*",
        "*mutter*",
        "*nngh*",
        "*pant*",
        "*retch*",
        "*shiver*",
        "*shrug*",
        "*shudder*",
        "*snarl*",
        "*snicker*",
        "*snore*",
        "*snort*",
        "*sob*",
        "*squeak*",
        "*yawn*",
        "*glug*",
        "*gag*",
        "*gasp*",
        "*hum*",
        "*moan*",
        "*boom*",
        "*crunch*",
        "*erp*",
        "*erf*",
        "*cough*",
    ],
)
def test_vocal_sound_rule_silences_recognized_sound_words(text: str):
    config = _vocal_sound_config()
    assert preprocess_text(text, config) == ""


def test_vocal_sound_rule_is_case_insensitive():
    config = _vocal_sound_config()
    assert preprocess_text("*Burp!*", config) == ""
    assert preprocess_text("*BURP*", config) == ""
    assert preprocess_text("*Sigh*", config) == ""


def test_vocal_sound_rule_allows_trailing_exclamation_and_whitespace():
    config = _vocal_sound_config()
    assert preprocess_text("*giggle!*", config) == ""
    assert preprocess_text("*whistle *", config) == ""


def test_vocal_sound_rule_removes_sound_but_keeps_surrounding_text():
    config = _vocal_sound_config()
    assert preprocess_text("*cough* Excuse me.", config) == " Excuse me."
    assert preprocess_text("He said *chuckle* nothing.", config) == "He said  nothing."


def test_vocal_sound_rule_does_not_match_unlisted_words():
    """Words not in the alternation (e.g. 'bang') are left untouched, as is a
    listed word with extra trailing text inside the asterisks (e.g. 'snort loudly')."""
    config = _vocal_sound_config()
    assert preprocess_text("*bang*", config) == "*bang*"
    assert preprocess_text("*snort loudly*", config) == "*snort loudly*"


def test_vocal_sound_rule_does_not_match_unrelated_suffixed_word():
    """'chuckling' still isn't matched, since only 'hic(?:cup(?:ping)?)?' was
    special-cased for a -ping suffix; other sound words remain suffix-sensitive."""
    config = _vocal_sound_config()
    assert preprocess_text("*chuckling*", config) == "*chuckling*"


def test_vocal_sound_rule_does_not_match_whinnying():
    """'whinny'/'whinnie'/'whinne' spellings are recognized, but the '-ing' form
    'whinnying' still isn't, since the fix only added the bare 'y'/'ie' endings."""
    config = _vocal_sound_config()
    assert preprocess_text("*whinnying*", config) == "*whinnying*"


# ==============================================================================
# convert_replacement (.NET $1 -> Python \g<1> backreferences)
# ==============================================================================


def test_convert_replacement_converts_single_dotnet_backreference():
    assert convert_replacement('"$1"') == '"\\g<1>"'


def test_convert_replacement_converts_multiple_dotnet_backreferences():
    assert convert_replacement("$1-$2") == "\\g<1>-\\g<2>"


def test_convert_replacement_converts_multi_digit_group_number():
    assert convert_replacement("$12") == "\\g<12>"


def test_convert_replacement_leaves_text_without_backreferences_unchanged():
    assert convert_replacement("no groups here") == "no groups here"


def test_convert_replacement_leaves_dollar_without_digits_unchanged():
    assert convert_replacement("$ not a group, $x either") == "$ not a group, $x either"


def test_convert_replacement_handles_empty_string():
    assert convert_replacement("") == ""


# ==============================================================================
# Asterisk-emphasis phonetic rule (regression: .NET $1 previously stayed literal)
# ==============================================================================


def _asterisk_emphasis_config(**overrides: Any) -> Dict[str, Any]:
    """Config with just the two rules relevant to the asterisk-emphasis bug:
    squashing repeated asterisks, then wrapping the remaining *text* in quotes."""
    return make_config(
        phoneticRules=[
            {
                "pattern": r"\*{2,}",
                "replacement": "*",
                "comment": "squash consecutive asterisks (two or more) into one",
            },
            {
                "pattern": r"\*([^*]+)\*",
                "replacement": '"$1"',
                "comment": "attempt to emphasize remaining text enclosed in asterisks",
            },
        ],
        **overrides,
    )


def test_asterisk_emphasis_rule_replaces_capture_group_not_literal_dollar_one():
    """
    Regression test for the reported bug: before convert_replacement was wired
    in, "$1" was left in the output verbatim instead of being substituted with
    the captured text.
    """
    config = _asterisk_emphasis_config()
    text = "** AAAAHHHH, THIS IS A MOST EXCELLENT TITHE, DARKLING. **"
    result = preprocess_text(text, config)
    assert result == '" AAAAHHHH, THIS IS A MOST EXCELLENT TITHE, DARKLING. "'
    assert "$1" not in result


def test_asterisk_emphasis_rule_with_single_asterisks():
    config = _asterisk_emphasis_config()
    result = preprocess_text("She said *hello there* to him.", config)
    assert result == 'She said "hello there" to him.'


def test_asterisk_emphasis_rule_with_multiple_pairs_in_one_string():
    config = _asterisk_emphasis_config()
    result = preprocess_text("*whisper* nothing else *shout*", config)
    assert result == '"whisper" nothing else "shout"'


def test_asterisk_emphasis_rule_leaves_text_without_asterisks_unchanged():
    config = _asterisk_emphasis_config()
    result = preprocess_text("No asterisks here.", config)
    assert result == "No asterisks here."


# ==============================================================================
# Token Cleanup
# ==============================================================================


def test_remaining_unknown_tokens_are_stripped():
    config = make_config()
    result = preprocess_text("Value: <UNKNOWN_TOKEN>.", config)
    assert result == "Value: ."


# ==============================================================================
# Full Pipeline
# ==============================================================================


def test_full_pipeline_combines_all_stages():
    config = make_config(
        pcGender="male",
        genderTokens={"HESHE": {"male": "he", "female": "she", "neutral": "they"}},
        phoneticRules=[{"pattern": r"—", "replacement": " ... "}],
    )
    text = "<CHARNAME> the <RACE> says <HESHE> will help—today."
    result = preprocess_text(text, config)
    assert result == "adventurer the person says he will help ... today."
