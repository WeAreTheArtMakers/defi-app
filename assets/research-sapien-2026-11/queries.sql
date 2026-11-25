-- WATAM Intelligence · SAPIEN order books, 17–25 Nov 2026 · queries behind https://defi.wearetheartmakers.com/research/sapien-order-books-2026-11/
-- Table: defi_chain.cex_depth_samples (5-minute samples of public spot order books). Data as of 25 Nov 2026 04:00 UTC.

-- SAPIEN (Base 0xc729777d0470f30612b1564fd96e8dd26f5814e3): hourly median of 5-min samples, weaker side within ±2 % of mid, USDT/USD books
SELECT toStartOfHour(sampled_at) AS h, venue, round(median(least(bid_usd_2, ask_usd_2))) AS weak2, round(median(mid), 5) AS mid
FROM defi_chain.cex_depth_samples
WHERE token = '0xc729777d0470f30612b1564fd96e8dd26f5814e3' AND symbol NOT LIKE '%USDC' AND status = 'ok' AND identity != 'mismatch'
  AND sampled_at >= '2026-11-17 00:00:00' AND sampled_at < '2026-11-25 04:00:00'
  AND venue IN ('binance','lbank','toobit','bingx','coinbase','kraken','kucoin','gate','bitget')
GROUP BY h, venue ORDER BY h, venue
FORMAT TSVWithNames

-- The step: every 5-min sample on the four books, 24 Nov 2026 09:55-10:20 UTC
SELECT venue, sampled_at, round(least(bid_usd_2, ask_usd_2)) AS weak2, round(bid_usd_2) AS bid2, round(ask_usd_2) AS ask2, mid, bid_levels, ask_levels, bid_floor_2, ask_floor_2
FROM defi_chain.cex_depth_samples
WHERE token = '0xc729777d0470f30612b1564fd96e8dd26f5814e3' AND symbol NOT LIKE '%USDC' AND status = 'ok' AND venue IN ('binance','lbank','toobit','bingx')
  AND sampled_at BETWEEN '2026-11-24 09:55:00' AND '2026-11-24 10:20:00'
ORDER BY venue, sampled_at
FORMAT TSVWithNames

SELECT venue,
  round(medianIf(least(bid_usd_2,ask_usd_2), sampled_at>='2026-11-17 00:00:00' AND sampled_at<'2026-11-20 00:00:00')) pre,
  round(medianIf(least(bid_usd_2,ask_usd_2), sampled_at>='2026-11-20 08:00:00' AND sampled_at<'2026-11-24 09:00:00')) during,
  round(medianIf(least(bid_usd_2,ask_usd_2), sampled_at>='2026-11-24 10:00:00')) after
FROM defi_chain.cex_depth_samples
WHERE token='0xc729777d0470f30612b1564fd96e8dd26f5814e3' AND symbol NOT LIKE '%USDC' AND status='ok' AND sampled_at>='2026-11-17 00:00:00'
GROUP BY venue ORDER BY pre DESC;
