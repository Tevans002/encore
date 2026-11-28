import json
import pytest
import states as st
import supervisor as sup

TABLE = ("ambient", "piano", "jazz", "folk", "acoustic", "classical")
ORDER = ["acoustic", "folk", "piano"]


def cfg():
    return st.Config(table_tags=TABLE, fallback_zone="America/Denver", default_prompt="ambient")


def ready(order=None):
    d = st.RunData(run_id="r1")
    d.city = "Missoula"
    d.forecast = []
    d.listens = {"status": "covered", "excluded_mbids": [], "in_window": 0, "unmapped": 0}
    if order is not None:
        st.set_tag_order(d, cfg(), order)
    return d


def ladder_state(order):
    d = ready(order)
    d.tags_used = ["acoustic"]
    d.radio_calls = 1
    d.same_tag_retried = True
    d.tracks = [{"x": 1}]
    d.select_reason = "no_set_in_window"
    return d


def test_set_tag_order_stores_a_copy():
    d = ready()
    order = list(ORDER)
    assert st.set_tag_order(d, cfg(), order) == ORDER
    order.append("jazz")
    assert d.tag_order == ORDER


@pytest.mark.parametrize("bad", [[], ["folk", "folk"], ["nope"], ["folk", "metal"], ["folk", 3]])
def test_set_tag_order_rejects_bad_orders(bad):
    d = ready()
    with pytest.raises(ValueError):
        st.set_tag_order(d, cfg(), bad)
    assert d.tag_order is None


def test_rejected_after_the_first_radio_call_and_when_weather_failed():
    d = ready()
    st.record_radio_call(d, cfg(), "folk", None)
    with pytest.raises(ValueError):
        st.set_tag_order(d, cfg(), ORDER)
    d2 = ready()
    d2.weather_failed = True
    with pytest.raises(ValueError):
        st.set_tag_order(d2, cfg(), ORDER)


def test_first_tag_in_call_radio_detail():
    s = st.next_step(ready(ORDER), cfg())
    assert (s.state, s.action) == ("CURATE", "call_radio")
    assert s.detail["first_tag"] == "acoustic" and s.detail["default_prompt"] is None


def test_unchanged_without_tag_order():
    s = st.next_step(ready(), cfg())
    assert "first_tag" not in s.detail


def test_artist_prompts_come_with_first_tag():
    d = ready(ORDER)
    st.add_artist(d, "Pearl Jam", "user")
    s = st.next_step(d, cfg())
    assert s.detail["artist_prompts"] == [st.artist_prompt("Pearl Jam")]
    assert s.detail["first_tag"] == "acoustic"


def test_no_first_tag_after_a_call_was_made():
    d = ready(ORDER)
    st.record_radio_call(d, cfg(), "acoustic", None)
    s = st.next_step(d, cfg())
    assert (s.state, s.action) == ("CURATE", "call_radio")
    assert "first_tag" not in s.detail


def test_first_radio_call_must_use_the_first_tag():
    d = ready(ORDER)
    with pytest.raises(ValueError):
        st.record_radio_call(d, cfg(), "folk", None)
    assert d.radio_calls == 0 and d.tags_used == []
    assert st.record_radio_call(d, cfg(), "acoustic", None) == "first"


def test_artist_prompt_is_still_allowed_first():
    d = ready(ORDER)
    p = st.add_artist(d, "Pearl Jam", "user")
    assert st.record_radio_call(d, cfg(), p, None) == "first"


def test_first_call_unchanged_without_order():
    d = ready()
    assert st.record_radio_call(d, cfg(), "jazz", None) == "first"


def test_ladder_follows_the_order_then_the_rest_of_the_table():
    s = st.next_step(ladder_state(ORDER), cfg())
    assert s.action == "different_tag"
    assert s.detail["candidates"] == ["folk", "piano", "ambient", "jazz", "classical"]


def test_ladder_unchanged_without_order():
    s = st.next_step(ladder_state(None), cfg())
    assert s.detail["candidates"] == ["ambient", "piano", "jazz", "folk", "classical"]


def test_options_offer_only_the_first_tag():
    opts = sup.options_for(st.next_step(ready(ORDER), cfg()), cfg())
    assert opts[0].prompts == ("acoustic",)


def test_options_with_artist_and_without_order():
    d = ready(ORDER)
    st.add_artist(d, "Pearl Jam", "user")
    assert sup.options_for(st.next_step(d, cfg()), cfg())[0].prompts == (st.artist_prompt("Pearl Jam"), "acoustic")
    assert sup.options_for(st.next_step(ready(), cfg()), cfg())[0].prompts == TABLE


def test_different_tag_options_follow_the_order():
    opts = sup.options_for(st.next_step(ladder_state(ORDER), cfg()), cfg())
    assert opts[0].prompts == ("folk", "piano", "ambient", "jazz", "classical")


def test_round_trip_and_old_checkpoints():
    d = ready(ORDER)
    assert st.RunData.from_dict(json.loads(json.dumps(d.to_dict()))).tag_order == ORDER
    old = d.to_dict()
    del old["tag_order"]
    assert st.RunData.from_dict(old).tag_order is None
