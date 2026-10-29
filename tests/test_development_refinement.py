from dataclasses import asdict, replace
import json

import pytest

from quantbot.ai.development import registered_selection
from quantbot.ai.development_strategies import development_candidates, development_provider
from quantbot.crypto.ai import CryptoAccount
from decimal import Decimal
from tests.test_development_strategies import inputs, weights


def test_preregistered_subset_roundtrips_json_and_rejects_changed_or_duplicate_definitions():
    candidate = development_candidates("crypto")[-1]
    definition = json.loads(json.dumps(asdict(candidate)))
    assert registered_selection("crypto", [definition]) == [candidate]
    with pytest.raises(ValueError, match="重复"):
        registered_selection("crypto", [definition, definition])
    with pytest.raises(ValueError, match="不一致"):
        registered_selection("crypto", [{**definition, "rsi_entry_ceiling": 55}])


def test_single_rsi_refinement_changes_entry_only():
    config, context, _ = inputs("crypto")
    account = CryptoAccount(Decimal(1000), {}, {})
    changed = replace(context, features={s: {**v, "rsi_14": 49} for s, v in context.features.items()})
    old = development_provider("crypto", "trend_pullback")
    new = development_provider("crypto", "trend_pullback_rsi50")
    assert set(weights(old, changed, account, config).values()) == {0}
    assert set(weights(new, changed, account, config).values()) == {config["risk"]["max_weight"]}
    boundary = replace(context, features={s: {**v, "rsi_14": 50} for s, v in context.features.items()})
    assert set(weights(new, boundary, account, config).values()) == {0}
    assert old.candidate.exit == new.candidate.exit
    assert old.candidate.cooldown_bars == new.candidate.cooldown_bars == 3
