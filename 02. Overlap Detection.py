# Databricks notebook source
# MAGIC %md # Overlap Detection
# MAGIC 
# MAGIC We now try to detect potentially overlapping pings using a buffer on a particular day. 

# COMMAND ----------

from pyspark.sql.functions import *
from mosaic import *
enable_mosaic(spark, dbutils)

# COMMAND ----------

cargos_indexed = (
  spark
  .read
  .table('ship2ship.cargos_indexed')
  .filter(col('timestamp').between('2018-01-31T00:00:00.000+0000','2018-01-31T23:59:00.000+0000'))
)
display(cargos_indexed)
cargos_indexed.count()

# COMMAND ----------

# MAGIC %md ## Buffering
# MAGIC 
# MAGIC 1. Convert the point into a polygon by buffering it with a certain area to turn this into a circle. 
# MAGIC 2. Index the polygon to leverage more performant querying. 
# MAGIC 
# MAGIC ![](http://1fykyq3mdn5r21tpna3wkdyi-wpengine.netdna-ssl.com/wp-content/uploads/2018/06/image14-1.png)
# MAGIC 
# MAGIC 
# MAGIC 
# MAGIC Choosing `(1*0.001 - 1*0.0001)` as being equal to 99.99 metres at the equator
# MAGIC Ref: http://wiki.gis.com/wiki/index.php/Decimal_degrees

# COMMAND ----------

one_metre = (0.00001 - 	0.000001)
buffer = 100 * one_metre 

(
  cargos_indexed
  .withColumn('buffer_geom', st_buffer("point_geom", lit(buffer)))
  .withColumn("ix", mosaic_explode("buffer_geom", lit(9)))
  .write
  .mode('overwrite')
  .saveAsTable('ship2ship.cargos_buffered')
)

# COMMAND ----------

# DBTITLE 1,We can optimise our table to colocate data and make querying faster
# MAGIC %sql
# MAGIC OPTIMIZE ship2ship.cargos_buffered ZORDER BY (ix.index_id, timestamp);
# MAGIC SELECT * FROM ship2ship.cargos_buffered;

# COMMAND ----------

# MAGIC %md ## Implement Algorithm

# COMMAND ----------

buffered_events = (spark.read
  .table('ship2ship.cargos_buffered')
  .repartition(sc.defaultParallelism * 20)
)

def ts_diff(ts1, ts2):
  return abs(col(ts1).cast('long') - col(ts2).cast('long'))
          
def time_window(sog1, sog2, heading1, heading2, radius):
  v_x1 = col(sog1)*cos(col(heading1))
  v_y1 = col(sog1)*sin(col(heading1))
  v_x2 = col(sog2)*cos(col(heading2))
  v_y2 = col(sog2)*sin(col(heading2))
  v_relative = sqrt((v_x1 + v_x2)*(v_x1 + v_x2) + (v_y1 + v_y2)*(v_y1 + v_y2))    # compute relative vectors speed based x and y partial speeds
  return v_relative * lit(1000) / lit(radius) / lit(3600)                         # convert to m/s and determine ratio between speed and radius 

candidates = (
  buffered_events.alias("a")
  .join(
    buffered_events.alias("b"),
    [col("a.ix.index_id") == col("b.ix.index_id"),                                 # to only compare across efficient indices
     col('a.mmsi') < col('b.mmsi'),                                                # to prevent comparing candidates bidirectionally
     ts_diff('a.timestamp', 'b.timestamp') < 
       time_window("a.sog_kmph", "b.sog_kmph", "a.heading", "b.heading", buffer)
    ]
  )
  .where(
    (col('a.ix.is_core') |  col('b.ix.is_core'))                                   # if either candidate fully covers an index, no further comparison is needed
    | st_intersects('a.ix.wkb', 'b.ix.wkb')                                        # limit geospatial querying to cases where indices are not enough
  )
  .select(
    col('a.vesselName').alias('vessel_1'),
    col('b.vesselName').alias('vessel_2'),
    col('a.timestamp').alias('timestamp_1'),
    col('b.timestamp').alias('timestamp_2'),
    col('a.ix.index_id').alias('ix')
  )
)

# COMMAND ----------

(
  candidates
   .write
   .mode('overwrite')
   .saveAsTable('ship2ship.overlap_candidates')
)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT * FROM ship2ship.overlap_candidates

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE TEMPORARY VIEW agg_overlap AS
# MAGIC SELECT ix, count(*) AS count
# MAGIC FROM ship2ship.overlap_candidates 
# MAGIC GROUP BY ix, vessel_1, vessel_2 
# MAGIC ORDER BY count DESC 

# COMMAND ----------

display(spark.read.table("agg_overlap"))

# COMMAND ----------

# MAGIC %%mosaic_kepler
# MAGIC "agg_overlap" "ix" "h3" 10000

# COMMAND ----------


