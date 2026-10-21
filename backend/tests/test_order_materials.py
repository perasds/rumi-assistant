import json
import pytest


@pytest.mark.external_materials
def test_catalog_preserves_all_verified_recipes_exact_levels_and_current_work_amount(external_materials_root):
    from rumi_market.order_catalog import load_order_catalog
    catalog = load_order_catalog()
    public = json.loads((external_materials_root / 'artifacts/rumi/can-food-analysis/order-materials.public.json').read_text(encoding='utf-8'))
    assert len(catalog['recipes']) == 37
    assert len({row['building_id'] for row in catalog['recipes']}) == 21
    actual = {(row['building_id'], row['recipe_id']): row for row in catalog['recipes']}
    for row in public['entries']:
        config = actual[row['building_id'], row['recipe_id']]
        assert config['available_levels'] == row['available_building_levels']
        assert config['work_amount'] == row['raw_work_amount']
        assert config['materials'] == [{'item_id': item['item_id'], 'count': item['count']} for item in row['inputs']]
        assert config['products'] == [{'item_id': item['item_id'], 'count': item['count']} for item in row['outputs']]


def test_multilevel_requirements_use_intermediate_inventory_before_raw_inputs():
    from rumi_market.order_planner import expand_material_requirements
    catalog = {'recipes': [
        {'recipe_id': 'wood', 'materials': [], 'products': [{'item_id': 'wood', 'count': 1}]},
        {'recipe_id': 'plank', 'materials': [{'item_id': 'wood', 'count': 5}], 'products': [{'item_id': 'plank', 'count': 2}]},
        {'recipe_id': 'joint', 'materials': [{'item_id': 'plank', 'count': 3}], 'products': [{'item_id': 'joint', 'count': 1}]},
    ]}
    # 2 joints need 6 planks; 3 existing planks leave 2 batches = 10 wood, 4 held wood leave 6.
    result = expand_material_requirements({'joint': 2}, {'plank': 3, 'wood': 4}, catalog)
    assert result == {'wood': 6}


def test_shared_materials_and_batch_surplus_are_not_allocated_twice():
    from rumi_market.order_planner import expand_material_requirements
    catalog = {'recipes': [
        {'recipe_id': 'wood', 'materials': [], 'products': [{'item_id': 'wood', 'count': 1}]},
        {'recipe_id': 'plank', 'materials': [{'item_id': 'wood', 'count': 5}], 'products': [{'item_id': 'plank', 'count': 2}]},
    ]}
    assert expand_material_requirements({'plank': 3, 'wood': 2}, {'wood': 6}, catalog) == {'wood': 6}


def test_bom_cycle_is_reported_instead_of_recursing_forever():
    import pytest
    from rumi_market.order_planner import expand_material_requirements
    catalog = {'recipes': [{'recipe_id': 'loop', 'materials': [{'item_id': 'x', 'count': 1}],
                            'products': [{'item_id': 'x', 'count': 1}]}]}
    with pytest.raises(ValueError, match='cycle'):
        expand_material_requirements({'x': 1}, {}, catalog)
