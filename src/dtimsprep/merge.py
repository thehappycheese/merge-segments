from typing import Optional, List
from enum import Enum
import pandas as pd
import numpy as np


class AggregationType(Enum):
	KeepLongest = 1
	Average = 2
	LengthWeightedAverage = 3
	LengthWeightedPercentile = 4


class Aggregation:

	def __init__(self, aggregation_type:AggregationType, percentile:Optional[float] = None):
		"""Don't use initialise this class directly, please use one of the static factory functions above"""
		self.type:AggregationType = aggregation_type
		self.percentile:Optional[float] = percentile
		pass

	@staticmethod
	def KeepLongest():
		return Aggregation(AggregationType.KeepLongest)

	@staticmethod
	def LengthWeightedAverage():
		return Aggregation(AggregationType.LengthWeightedAverage)

	@staticmethod
	def Average():
		return Aggregation(AggregationType.Average)

	@staticmethod
	def LengthWeightedPercentile(percentile:float):
		if percentile > 1.0 or percentile < 0.0:
			raise ValueError(
				f"Percentile out of range. Must be greater than 0.0 and less than 1.0. Got {percentile}." +
				(" Did you need to divide by 100?" if percentile>1.0 else "")
			)
		return Aggregation(
			AggregationType.LengthWeightedPercentile,
			percentile=percentile
		)


class Action:
	def __init__(
			self,
			column_name: str,
			aggregation: Aggregation,
			rename:Optional[str] = None
	):
		self.column_name: str = column_name
		self.rename = rename if rename is not None else self.column_name
		self.aggregation: Aggregation = aggregation


def on_slk_intervals(target: pd.DataFrame, data: pd.DataFrame, join_left: List[str], column_actions: List[Action],from_to:List[str]=["slk_from", "slk_to"]):
	result_index = []
	result_rows = []

	# precalculate slk_length for each row of data
	# data.loc[:, CN.slk_length] = data[CN.slk_to] - data[CN.slk_from]

	# reindex data for faster lookup
	data['data_id'] = data.index
	data = data.set_index([*join_left, 'data_id'])
	data = data.sort_index()

	# keep_column_names = [column_action.column_name for column_action in column_actions]

	# Group target data by Road Number and Carriageway
	try:
		target_groups = target.groupby(join_left)
	except KeyError as e:
		matching_columns = [col for col in join_left if col in target.columns]
		raise Exception(f"Parameter join_left={join_left} did not match" + (
			" any columns in the target DataFrame" if len(matching_columns)==0
			else f" all columns in target DataFrame. Only matched columns {matching_columns}"
		))

	for target_group_index, target_group in target_groups:
		


		try:
			data_matching_target_group = data.loc[target_group_index]
		except KeyError:
			# There was no data matching the target group. Skip adding output. output to these rows will be NaN for all columns.
			continue
		except TypeError as e:
			# The datatype of group_index is picky... sometimes it wants a tuple, sometimes it will accept a list
			# this appears to be a bug or inconsistency with pandas when using multi-index dataframes.
			print(f"Error: Could not group the following data by {group_index}:")
			print(f"type(group_index)  {type(group_index)}:")
			print(f"type(data_matching_target_group)  {type(data_matching_target_group)}:")
			print("the data:")
			print(data)
			raise e


		for target_index, target_row in target_group.iterrows():
			data_to_aggregate_for_target_group = data_matching_target_group[
				(data_matching_target_group[CN.slk_from] < target_row[CN.slk_to]) &
				(data_matching_target_group[CN.slk_to] > target_row[CN.slk_from])
			].copy()

			# if no data matches the target group then skip
			if data_to_aggregate_for_target_group.empty:
				continue

			# compute overlap metrics for each row of data
			overlap_min = np.maximum(data_to_aggregate_for_target_group[CN.slk_from], target_row[CN.slk_from])
			overlap_max = np.minimum(data_to_aggregate_for_target_group[CN.slk_to],   target_row[CN.slk_to])

			# the maximum in this line might optionally be remove since it is guaranteed by an earlier filter
			# overlap_len = np.maximum(overlap_max - overlap_min, 0)
			overlap_len = overlap_max - overlap_min

			# expect this to trigger warning
			data_to_aggregate_for_target_group["overlap_len"] = overlap_len

			# for each column of data that we keep, we must aggregate each field down to a single value
			# create a blank row to store the result of each column
			aggregated_result_row = []
			for column_action_index, column_action in enumerate(column_actions):
				column_len_to_aggregate = data_to_aggregate_for_target_group.loc[:,[column_action.column_name,"overlap_len"]]
				column_len_to_aggregate = column_len_to_aggregate[
					~column_len_to_aggregate.iloc[:,0].isna() &
					(column_len_to_aggregate["overlap_len"]>0)
				]

				if column_len_to_aggregate.empty:
					# Infill with none or we will lose our column position.
					aggregated_result_row.append(None)
					continue

				column_to_aggregate = column_len_to_aggregate.iloc[:,0]
				column_to_aggregate_overlap_len = column_len_to_aggregate.iloc[:,1]

				if  column_action.aggregation.type == AggregationType.Average:
					aggregated_result_row.append(
						column_to_aggregate.mean()
					)
				elif column_action.aggregation.type == AggregationType.LengthWeightedAverage:
					total_overlap_length = column_to_aggregate_overlap_len.sum()
					aggregated_result_row.append(
						(column_to_aggregate * column_to_aggregate_overlap_len).sum() / total_overlap_length
					)

				elif column_action.aggregation.type == AggregationType.KeepLongest:
					aggregated_result_row.append(
						column_to_aggregate.loc[column_to_aggregate_overlap_len.idxmax()]
					)

				elif column_action.aggregation.type == AggregationType.LengthWeightedPercentile:

					column_len_to_aggregate.sort_values(
						by=column_action.column_name,
						ascending=True
					)
					column_to_aggregate = column_len_to_aggregate.iloc[:,0]
					column_to_aggregate_overlap_len = column_len_to_aggregate.iloc[:,1]

					x_coords = (column_to_aggregate_overlap_len.rolling(2).mean()).fillna(0).cumsum()
					x_coords /= x_coords.iloc[-1]
					result = np.interp(
						column_action.aggregation.percentile,
						x_coords.to_numpy(),
						column_to_aggregate
					)
					aggregated_result_row.append(result)

			result_index.append(target_index)
			result_rows.append(aggregated_result_row)

	return target.join(
		pd.DataFrame(
			result_rows,
			columns=[x.rename for x in column_actions],
			index=result_index
		)
	)