from collector.services.text import content_hash, hamming_distance, normalize_url, simhash64, title_similarity


def test_url_normalization_removes_tracking_and_fragment():
    assert normalize_url("HTTPS://Example.COM:443/news//item/?utm_source=x&b=2&a=1#part") == "https://example.com/news/item?a=1&b=2"


def test_multilingual_simhash_is_stable():
    text = "Хорошая новость о новом общественном парке и работе волонтеров. " * 10
    assert simhash64(text) == simhash64(text)
    assert hamming_distance(simhash64(text), simhash64(text + " Сегодня.")) <= 10
    assert content_hash(text) == content_hash(text.upper())
    assert title_similarity("Новый парк открыт", "Новый парк открыт!") > 0.9

