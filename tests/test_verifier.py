from sop.insurance.verifier import FACTORS, normalize, verify


def check(repo, **raw):
    provided = {k: normalize(k, v) for k, v in raw.items()}
    assert all(provided.values()), f"unusable test input: {provided}"
    return verify(provided, repo.policyholders)


def test_canonical_caller_verifies(repo):
    result = check(repo, full_name="Margaret Chen", dob="1985-03-15", id_last4="4472")
    assert (result.status, result.party_id) == ("verified", "P9")


def test_two_factors_are_not_enough(repo):
    assert check(repo, full_name="Margaret Chen", dob="1985-03-15").status == "insufficient"


def test_policy_number_is_not_a_factor():
    assert "policy_number" not in FACTORS


def test_phone_formats_normalize(repo):
    result = check(repo, full_name="margaret  CHEN", dob="1985-03-15", phone="(650) 521-2836")
    assert result.party_id == "P9"


def test_near_identical_phone_does_not_match(repo):
    # P13's phone differs from P9's only in the last digit.
    assert check(repo, full_name="Margaret Chen", dob="1985-03-15", phone="+1 650 521 2830").status == "failed"


def test_one_contradicting_factor_fails_closed(repo):
    result = check(repo, full_name="Margaret Chen", dob="1985-03-15", id_last4="4472", phone="650-000-0000")
    assert result.status == "failed"


def test_factors_from_different_customers_fail(repo):
    # Margaret's name + Ava's DOB and ID.
    assert check(repo, full_name="Margaret Chen", dob="1990-08-21", id_last4="9180").status == "failed"


def test_national_id_counts_and_name_order_is_flexible(repo):
    result = check(repo, full_name="Tian Ma", dob="1964-09-10", id_last4="6688")
    assert (result.status, result.party_id) == ("verified", "P12")


def test_name_and_email_aliases(repo):
    assert check(repo, full_name="Yaven Li", email="YaWen.Li@example.com", dob="1989-12-03").party_id == "P13"
    assert check(repo, full_name="Li Yawen", phone="650-521-2830", dob="1989-12-03").party_id == "P13"


def test_unusable_values_are_rejected():
    assert normalize("full_name", "Margaret") is None  # first name only
    assert normalize("phone", "521-2836") is None
    assert normalize("dob", "03/15/1985") is None  # the NLU must produce ISO dates
    assert normalize("id_last4", "44721") is None
    assert normalize("email", "margaret-at-email") is None
