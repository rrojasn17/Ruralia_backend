from types import SimpleNamespace
from modules.mod_floresvolcan.technical_materials import reference_for


def product(**changes):
    return SimpleNamespace(
        **(
            {
                "variety": "Bach",
                "caliber": "12/14",
                "common_code": "LA",
                "default_density": None,
                "default_cycle_weeks": None,
            }
            | changes
        )
    )


def test_group_reference_is_explicitly_estimated_and_preserves_existing_values():
    result = reference_for(product())
    assert result["updates"]["default_density"] == 45
    assert result["updates"]["default_cycle_weeks"] == 12
    assert all(e["estimated"] for e in result["evidence"].values())
    assert (
        reference_for(product(default_density=73, default_cycle_weeks=11))["updates"]
        == {}
    )


def test_no_extrapolation_double_nose_or_unsupported_size():
    for size in ["DN 18/20", "20/22 DN", "11/12", "15/17", "mal formado"]:
        assert "default_density" not in reference_for(product(caliber=size))["updates"]
    assert reference_for(product(caliber="12,5/14"))["updates"]["default_density"] == 45


def test_named_cut_flower_sources_and_unknown_group():
    angela = reference_for(
        product(variety="Roselily Angela", caliber="15/17", common_code="RL")
    )
    assert angela["updates"]["default_cycle_weeks"] == 16
    assert "default_density" not in angela["updates"]
    alvaro = reference_for(
        product(variety="Starlily Alvaro", caliber="14/16", common_code="RL")
    )
    assert alvaro["family"] == "OT" and alvaro["updates"]["default_cycle_weeks"] == 14
    assert reference_for(product(common_code="DESCONOCIDO"))["updates"] == {}
