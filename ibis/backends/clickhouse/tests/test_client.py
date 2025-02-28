import pandas as pd
import pandas.testing as tm
import pytest
from clickhouse_driver.dbapi import OperationalError
from pytest import param

import ibis
import ibis.expr.datatypes as dt
import ibis.expr.types as ir
from ibis import config
from ibis.common.exceptions import IbisError
from ibis.util import gen_name

pytest.importorskip("clickhouse_driver")


def test_run_sql(con):
    query = 'SELECT * FROM ibis_testing.functional_alltypes'
    table = con.sql(query)

    fa = con.table('functional_alltypes')
    assert isinstance(table, ir.Table)
    assert table.schema() == fa.schema()

    expr = table.limit(10)
    result = expr.execute()
    assert len(result) == 10


def test_get_schema(con):
    t = con.table('functional_alltypes')
    schema = con.get_schema('functional_alltypes')
    assert t.schema() == schema


def test_result_as_dataframe(con, alltypes):
    expr = alltypes.limit(10)

    ex_names = list(expr.schema().names)
    result = con.execute(expr)

    assert isinstance(result, pd.DataFrame)
    assert result.columns.tolist() == ex_names
    assert len(result) == 10


def test_array_default_limit(con, alltypes):
    result = con.execute(alltypes.float_col, limit=100)
    assert len(result) == 100


def test_limit_overrides_expr(con, alltypes):
    result = con.execute(alltypes.limit(10), limit=5)
    assert len(result) == 5


def test_limit_equals_none_no_limit(alltypes):
    with config.option_context('sql.default_limit', 10):
        result = alltypes.execute(limit=None)
        assert len(result) > 10


def test_verbose_log_queries(con):
    queries = []

    def logger(x):
        queries.append(x)

    with config.option_context('verbose', True):
        with config.option_context('verbose_log', logger):
            con.table('functional_alltypes')

    expected = 'DESCRIBE ibis_testing.functional_alltypes'

    assert len(queries) == 1
    assert queries[0] == expected


def test_sql_query_limits(alltypes):
    table = alltypes
    with config.option_context('sql.default_limit', 100000):
        # table has 25 rows
        assert len(table.execute()) == 7300
        # comply with limit arg for Table
        assert len(table.execute(limit=10)) == 10
        # state hasn't changed
        assert len(table.execute()) == 7300
        # non-Table ignores default_limit
        assert table.count().execute() == 7300
        # non-Table doesn't observe limit arg
        assert table.count().execute(limit=10) == 7300
    with config.option_context('sql.default_limit', 20):
        # Table observes default limit setting
        assert len(table.execute()) == 20
        # explicit limit= overrides default
        assert len(table.execute(limit=15)) == 15
        assert len(table.execute(limit=23)) == 23
        # non-Table ignores default_limit
        assert table.count().execute() == 7300
        # non-Table doesn't observe limit arg
        assert table.count().execute(limit=10) == 7300
    # eliminating default_limit doesn't break anything
    with config.option_context('sql.default_limit', None):
        assert len(table.execute()) == 7300
        assert len(table.execute(limit=15)) == 15
        assert len(table.execute(limit=10000)) == 7300
        assert table.count().execute() == 7300
        assert table.count().execute(limit=10) == 7300


def test_embedded_identifier_quoting(alltypes):
    t = alltypes

    expr = t[[(t.double_col * 2).name('double(fun)')]]['double(fun)'].sum()
    expr.execute()


@pytest.fixture
def temporary_alltypes(con):
    table = gen_name("temporary_alltypes")
    con.raw_sql(f"CREATE TABLE {table} AS functional_alltypes")
    yield con.table(table)
    con.drop_table(table)


def test_insert(temporary_alltypes, df):
    temporary = temporary_alltypes
    records = df[:10]

    assert temporary.count().execute() == 0
    temporary.insert(records)

    tm.assert_frame_equal(temporary.execute(), records)


def test_insert_with_less_columns(temporary_alltypes, df):
    temporary = temporary_alltypes
    records = df.loc[:10, ['string_col']].copy()
    records['date_col'] = None

    with pytest.raises(AssertionError):
        temporary.insert(records)


def test_insert_with_more_columns(temporary_alltypes, df):
    temporary = temporary_alltypes
    records = df[:10].copy()
    records['non_existing_column'] = 'raise on me'

    with pytest.raises(AssertionError):
        temporary.insert(records)


@pytest.mark.parametrize(
    ("query", "expected_schema"),
    [
        (
            "SELECT 1 as a, 2 + dummy as b",
            ibis.schema(dict(a=dt.UInt8(nullable=False), b=dt.UInt16(nullable=False))),
        ),
        (
            "SELECT string_col, sum(double_col) as b FROM functional_alltypes GROUP BY string_col",
            ibis.schema(
                dict(
                    string_col=dt.String(nullable=True),
                    b=dt.Float64(nullable=True),
                )
            ),
        ),
    ],
)
def test_get_schema_using_query(con, query, expected_schema):
    result = con._get_schema_using_query(query)
    assert result == expected_schema


def test_list_tables_database(con):
    tables = con.list_tables()
    tables2 = con.list_tables(database=con.current_database)
    # some overlap, but not necessarily identical because
    # a database may have temporary tables added/removed between list_tables
    # calls
    assert set(tables) & set(tables2)


@pytest.fixture
def temp_db(con, worker_id):
    dbname = f"clickhouse_create_database_{worker_id}"
    con.create_database(dbname, force=True)
    yield dbname
    con.drop_database(dbname, force=True)


def test_list_tables_empty_database(con, temp_db):
    assert not con.list_tables(database=temp_db)


@pytest.mark.parametrize(
    "temp",
    [
        param(
            True,
            marks=pytest.mark.xfail(
                reason="Ibis is likely making incorrect assumptions about object lifetime and cursors",
                raises=IbisError,
            ),
        ),
        False,
    ],
    ids=["temp", "no_temp"],
)
def test_create_table_no_data(con, temp, temp_table):
    schema = ibis.schema(dict(a="!int", b="string"))
    t = con.create_table(temp_table, schema=schema, temp=temp, engine="Memory")
    assert t.execute().empty


@pytest.mark.parametrize(
    "data",
    [
        {"a": [1, 2, 3], "b": [None, "b", "c"]},
        pd.DataFrame({"a": [1, 2, 3], "b": [None, "b", "c"]}),
    ],
    ids=["dict", "dataframe"],
)
@pytest.mark.parametrize(
    "engine",
    ["File(Native)", "File(Parquet)", "Memory"],
    ids=["native", "mem", "parquet"],
)
def test_create_table_data(con, data, engine, temp_table):
    schema = ibis.schema(dict(a="!int", b="string"))
    t = con.create_table(temp_table, obj=data, schema=schema, engine=engine)
    assert len(t.execute()) == 3


@pytest.mark.parametrize(
    "engine",
    [
        "File(Native)",
        param(
            "File(Parquet)",
            marks=pytest.mark.xfail(
                reason="Parquet file size is 0 bytes", raises=OperationalError
            ),
        ),
        "Memory",
    ],
    ids=["native", "mem", "parquet"],
)
def test_truncate_table(con, engine, temp_table):
    t = con.create_table(temp_table, obj={"a": [1]}, engine=engine)
    assert len(t.execute()) == 1
    con.truncate_table(temp_table)
    assert t.execute().empty
