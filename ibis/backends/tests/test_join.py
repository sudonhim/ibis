import sqlite3

import numpy as np
import pandas as pd
import pytest
from packaging.version import parse as vparse
from pytest import param

import ibis.common.exceptions as exc
import ibis.expr.schema as sch


def _pandas_semi_join(left, right, on, **_):
    assert len(on) == 1, str(on)
    inner = pd.merge(left, right, how="inner", on=on)
    filt = left.loc[:, on[0]].isin(inner.loc[:, on[0]])
    return left.loc[filt, :]


def _pandas_anti_join(left, right, on, **_):
    inner = pd.merge(left, right, how="left", indicator=True, on=on)
    return inner[inner["_merge"] == "left_only"]


def _merge(
    left,
    right,
    on,
    *,
    how,
    suffixes=("", "_y"),
):
    joined = pd.merge(
        left,
        right,
        on=on,
        how=how,
        suffixes=suffixes,
        indicator=True,
    )
    if how == "right":
        filt_key = {"left_only"}
    elif how == "left":
        filt_key = {"right_only"}
    else:
        filt_key = set()

    joined.loc[joined["_merge"].isin(filt_key), on] = np.nan
    return joined.drop(["_merge"], axis=1)


IMPLS = {
    "semi": _pandas_semi_join,
    "anti": _pandas_anti_join,
}


def check_eq(left, right, how, **kwargs):
    impl = IMPLS.get(how, _merge)
    return impl(left, right, how=how, **kwargs)


@pytest.mark.parametrize(
    "how",
    [
        "inner",
        "left",
        "right",
        param(
            "outer",
            # TODO: mysql will likely never support full outer join
            # syntax, but we might be able to work around that using
            # LEFT JOIN UNION RIGHT JOIN
            marks=pytest.mark.notimpl(
                ["mysql"]
                + (["sqlite"] * (vparse(sqlite3.sqlite_version) < vparse("3.39")))
            ),
        ),
    ],
)
@pytest.mark.notimpl(["datafusion", "druid"])
def test_mutating_join(backend, batting, awards_players, how):
    left = batting[batting.yearID == 2015]
    right = awards_players[awards_players.lgID == 'NL'].drop('yearID', 'lgID')

    left_df = left.execute()
    right_df = right.execute()
    predicate = ['playerID']
    result_order = ['playerID', 'yearID', 'lgID', 'stint']

    expr = left.join(right, predicate, how=how)
    if how == "inner":
        result = (
            expr.execute()
            .fillna(np.nan)[left.columns]
            .sort_values(result_order)
            .reset_index(drop=True)
        )
    else:
        result = (
            expr.execute()
            .fillna(np.nan)
            .assign(
                playerID=lambda df: df.playerID.where(
                    df.playerID.notnull(),
                    df.playerID_right,
                )
            )
            .drop(['playerID_right'], axis=1)[left.columns]
            .sort_values(result_order)
            .reset_index(drop=True)
        )

    expected = (
        check_eq(
            left_df,
            right_df,
            how=how,
            on=predicate,
            suffixes=("_x", "_y"),
        )[left.columns]
        .sort_values(result_order)
        .reset_index(drop=True)
    )

    backend.assert_frame_equal(result, expected, check_like=True)


@pytest.mark.parametrize("how", ["semi", "anti"])
@pytest.mark.notimpl(["bigquery", "dask", "datafusion", "druid"])
def test_filtering_join(backend, batting, awards_players, how):
    left = batting[batting.yearID == 2015]
    right = awards_players[awards_players.lgID == 'NL'].drop('yearID', 'lgID')

    left_df = left.execute()
    right_df = right.execute()
    predicate = ['playerID']
    result_order = ['playerID', 'yearID', 'lgID', 'stint']

    expr = left.join(right, predicate, how=how)
    result = (
        expr.execute()
        .fillna(np.nan)
        .sort_values(result_order)[left.columns]
        .reset_index(drop=True)
    )

    expected = check_eq(
        left_df,
        right_df,
        how=how,
        on=predicate,
        suffixes=("", "_y"),
    ).sort_values(result_order)[list(left.columns)]

    backend.assert_frame_equal(result, expected, check_like=True)


@pytest.mark.notyet(
    ["pyspark"],
    reason="pyspark doesn't support joining on differing column names",
)
@pytest.mark.notimpl(["datafusion", "pyspark"])
def test_join_then_filter_no_column_overlap(awards_players, batting):
    left = batting[batting.yearID == 2015]
    year = left.yearID.name("year")
    left = left[year, "RBI"]
    right = awards_players[awards_players.lgID == 'NL']

    expr = left.join(right, left.year == right.yearID)
    filters = [expr.RBI == 9]
    q = expr.filter(filters)
    assert not q.execute().empty


@pytest.mark.notimpl(["datafusion"])
@pytest.mark.notyet(
    ["pyspark"],
    reason="pyspark doesn't support joining on differing column names",
)
def test_mutate_then_join_no_column_overlap(batting, awards_players):
    left = batting.mutate(year=batting.yearID).filter(lambda t: t.year == 2015)
    left = left["year", "RBI"]
    right = awards_players
    expr = left.join(right, left.year == right.yearID)
    assert not expr.limit(5).execute().empty


@pytest.mark.notimpl(["datafusion", "bigquery", "druid"])
@pytest.mark.notyet(
    ["pyspark"],
    reason="pyspark doesn't support joining on differing column names",
)
@pytest.mark.notyet(["dask"], reason="dask doesn't support descending order by")
def test_semi_join_topk(batting, awards_players):
    batting = batting.mutate(year=batting.yearID)
    left = batting.semi_join(batting.year.topk(5), "year").select("year", "RBI")
    expr = left.join(awards_players, left.year == awards_players.yearID)
    assert not expr.limit(5).execute().empty


@pytest.mark.notimpl(["dask", "datafusion", "druid"])
@pytest.mark.broken(
    ["duckdb"],
    raises=exc.IbisTypeError,
    reason="DuckDB as of 0.7.1 occasionally segfaults when there are `null`-typed columns present",
)
def test_join_with_pandas(batting, awards_players):
    batting_filt = batting[lambda t: t.yearID < 1900]
    awards_players_filt = awards_players[lambda t: t.yearID < 1900].execute()
    assert isinstance(awards_players_filt, pd.DataFrame)
    expr = batting_filt.join(awards_players_filt, "yearID")
    df = expr.execute()
    assert df.yearID.nunique() == 7


@pytest.mark.notimpl(["dask", "datafusion"])
def test_join_with_pandas_non_null_typed_columns(batting, awards_players):
    batting_filt = batting[lambda t: t.yearID < 1900][["yearID"]]
    awards_players_filt = awards_players[lambda t: t.yearID < 1900][
        ["yearID"]
    ].execute()

    # ensure that none of the columns of eitherr table have type null
    batting_schema = batting_filt.schema()
    assert len(batting_schema) == 1
    assert batting_schema["yearID"].is_integer()

    assert sch.infer(awards_players_filt) == sch.Schema(dict(yearID="int"))
    assert isinstance(awards_players_filt, pd.DataFrame)
    expr = batting_filt.join(awards_players_filt, "yearID")
    df = expr.execute()
    assert df.yearID.nunique() == 7
