"""The workflows module's JSONLogic subset (`aios.workflows.structured`): what conditions on
edges may say, and what they evaluate to. Saving and running them is in
`test_structured_workflows`."""

import pytest

from aios.workflows.structured import ConditionError, check_condition, evaluate

DATA = {"output": {"score": 7, "tags": ["a", "b"], "name": "Ada"}, "input": "go"}


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ({"var": "output.score"}, 7),
        ({"var": "output.tags.1"}, "b"),
        ({"var": ["output.missing", 3]}, 3),
        ({"var": "input"}, "go"),
        ({"==": [{"var": "output.score"}, "7"]}, True),
        ({"===": [{"var": "output.score"}, "7"]}, False),
        ({"!=": [1, 2]}, True),
        ({"!==": [1, 1]}, False),
        ({">": [{"var": "output.score"}, 5]}, True),
        ({">=": [7, 7]}, True),
        ({"<": [1, {"var": "output.score"}, 10]}, True),
        ({"<=": [1, 1, 0]}, False),
        ({"!": [[]]}, True),
        ({"!!": ["0"]}, True),
        ({"and": [True, 0, 1]}, 0),
        ({"or": [0, "", "x"]}, "x"),
        ({"if": [False, "a", {"var": "output.score"}, "b", "c"]}, "b"),
        ({"in": ["a", {"var": "output.tags"}]}, True),
        ({"in": ["d", "Ada"]}, True),
        ({"missing": ["output.score", "output.nope"]}, ["output.nope"]),
        ({"missing_some": [1, ["output.score", "output.nope"]]}, []),
        ({"missing_some": [2, ["output.score", "output.nope"]]}, ["output.nope"]),
    ],
)
def test_the_subset_evaluates_as_jsonlogic_does(expr, expected):
    assert evaluate(expr, DATA) == expected


@pytest.mark.parametrize(
    "expr",
    [
        {">": [{"var": "output.nope"}, 5]},  # null can't be ordered
        {"<": [{"var": "output.tags"}, 5]},
        {"in": ["a", 5]},
        {"if": []},
    ],
)
def test_what_cant_be_evaluated_raises(expr):
    with pytest.raises(ConditionError):
        evaluate(expr, DATA)


OUTPUT = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "detail": {"type": "object", "properties": {"level": {"type": "string"}}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}
INPUT = {"type": "object", "properties": {"region": {"type": "string"}}}


def test_saving_checks_operators_size_and_fields():
    ok = {"and": [{">": [{"var": "output.score"}, 5]}, {"==": [{"var": "input.region"}, "EU"]}]}
    assert check_condition(ok, input_schema=INPUT, output_schema=OUTPUT) == []
    assert check_condition({"var": "output.detail.level"}, INPUT, OUTPUT) == []
    assert check_condition({"var": "output.tags.0"}, INPUT, OUTPUT) == []

    [error] = check_condition({"+": [1, 2]}, INPUT, OUTPUT)
    assert "+" in error and "not allowed" in error
    [error] = check_condition({"method": ["x", "toUpperCase"]}, INPUT, OUTPUT)
    assert "not allowed" in error

    [error] = check_condition({"var": "output.scor"}, INPUT, OUTPUT)
    assert "output.scor" in error
    [error] = check_condition({"var": "input.country"}, INPUT, OUTPUT)
    assert "input.country" in error
    # Only `output` and `input` can be read.
    assert check_condition({"var": "other"}, INPUT, OUTPUT)
    # Paths are literals: no expression picks the field.
    assert check_condition({"var": {"if": [True, "output.score", "x"]}}, INPUT, OUTPUT)

    # Without a schema, only the whole value can be read.
    assert check_condition({"==": [{"var": "output"}, "yes"]}, None, None) == []
    [error] = check_condition({"var": "output.score"}, None, None)
    assert "no output schema" in error
    [error] = check_condition({"var": "input.region"}, None, None)
    assert "no input schema" in error

    huge = {"or": [{"==": [{"var": "output.score"}, n]} for n in range(200)]}
    [error] = check_condition(huge, INPUT, OUTPUT)
    assert "too large" in error
