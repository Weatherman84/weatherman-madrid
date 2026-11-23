"""Preview compact Shadow columns without a database or live inference."""

from datetime import date

import pandas as pd
import streamlit as st

from weatherman.madrid_late_shadow_display import (
    bucket_shadow_value,
    checkpoint_shadow_value,
    load_bundle,
    probabilities,
    table_view_for_target,
)


st.set_page_config(page_title="Madrid Shadow table preview", layout="wide")
st.title("Madrid · existing tables with v11 Shadow column")
st.caption(
    "Layout preview with archived research values only. No production database or live inference."
)
target = st.date_input("Target date", value=date(2026, 10, 8))
view = table_view_for_target(target, load_bundle())
labels = ["D-1 Evening @20:00", "D0 Morning @09:00", "First Live @12:00", "Late Live @16:00"]
st.subheader("Fixed decision checkpoints")
st.dataframe(
    pd.DataFrame(
        {
            "Checkpoint": labels,
            "v11 Shadow · Late": [checkpoint_shadow_value(view, x) for x in labels],
        }
    ),
    hide_index=True,
    width="stretch",
)
st.subheader("Bucket probabilities")
if view["record"]:
    champion = probabilities(view["record"]["champion"])
    buckets = sorted(set(champion) | set(view["probabilities"]))
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Bucket": f"{k} °C",
                    "Archived Champion": f"{champion.get(k, 0):.1%}",
                    "v11 Shadow · Late": bucket_shadow_value(view, k),
                }
                for k in buckets
            ]
        ),
        hide_index=True,
        width="stretch",
    )
else:
    st.dataframe(
        pd.DataFrame({"Bucket": ["—"], "Archived Champion": ["—"], "v11 Shadow · Late": ["—"]}),
        hide_index=True,
        width="stretch",
    )
st.caption(view["caption"])
