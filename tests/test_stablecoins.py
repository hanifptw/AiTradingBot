from src.market.stablecoins import is_stable_or_wrapped


def test_excludes_usdt_dai_etc():
    for s in ["USDT", "usdc", "DAI", "FDUSD"]:
        assert is_stable_or_wrapped(s)


def test_excludes_wrapped_eth_btc():
    for s in ["WBTC", "stETH", "WETH"]:
        assert is_stable_or_wrapped(s)


def test_keeps_btc_eth_sol():
    for s in ["BTC", "ETH", "SOL", "ADA"]:
        assert not is_stable_or_wrapped(s)
