from arxiv_latex_translator.pricing import token_cost


def test_gpt_6_2_sol_cost_uses_cached_input_as_a_subset() -> None:
    result = token_cost(100, 20, 30, model="gpt-6.2-sol")

    assert result["uncached_input_tokens"] == 80
    assert result["cost_usd"] == 0.000462
    assert result["pricing_rates_usd_per_million"]["cached_input_usd_per_million"] == 0.1


def test_gpt_6_sol_has_its_own_cached_input_rate() -> None:
    result = token_cost(100, 20, 30, model="gpt-6-sol")

    assert result["cost_usd"] == 0.000464


def test_unknown_model_does_not_claim_a_price() -> None:
    result = token_cost(100, 20, 30, model="custom-model")

    assert result["cost_usd"] is None
    assert result["pricing_available"] is False
