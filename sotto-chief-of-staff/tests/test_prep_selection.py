"""Research selection quality considers the primary person rather than related context."""
import prep_selection


def test_profile_quality_requires_substantial_primary_facts():
    # The changed packed format must not trigger paid re-research just because the first fact is short.
    assert prep_selection.is_high_quality_profile('Alex\n= Runs clinics.\n= Advises procurement boards on pediatric rollouts and governance.')
    assert not prep_selection.is_high_quality_profile('Alex\n= Runs clinics.\nRelated context about Jo: ' + 'expert ' * 30)

