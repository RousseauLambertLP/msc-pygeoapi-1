# How to output a png

```bash
curl -o test.png -X POST "http://geomet-dev-03.cmc.ec.gc.ca:5000/processes/generate-vigilance/jobs?raw=True" -H  "accept: application/json" -H  "Content-Type: application/json" -d "{\"inputs\":[{\"id\":\"layers\",\"value\":\"GEPS.DIAG.24_T8.ERGE15,GEPS.DIAG.24_T8.ERGE20,GEPS.DIAG.24_T8.ERGE25\"},{\"id\":\"forecast-hour\",\"value\":\"2020-07-05T00:00:00Z\"},{\"id\":\"model-run\",\"value\":\"2020-07-03T00:00:00Z\"},{\"id\":\"bbox\",\"value\":\"-158.203125, 83.215693, -44.296875, 36.879621\"},{\"id\":\"format\",\"value\":\"png\"}]}"
```

## Modification needed in msc-pygeoapi

```bash
diff --git a/pygeoapi/api.py b/pygeoapi/api.py
index 90d5072..f382443 100644
--- a/pygeoapi/api.py
+++ b/pygeoapi/api.py
@@ -1326,7 +1326,8 @@ class API:
                 response = outputs
             else:
                 response['outputs'] = outputs
-            return headers_, 201, json.dumps(response)
+                response = json.dumps(response)
+            return headers_, 201, response
         except Exception as err:
             exception = {
                 'code': 'InvalidParameterValue',
```

## Limitations

At the moment, the request needs to be done with `?raw=True` because we are returning bytes and not string and bytes can't be returned directly in a JSON objects.

## MSC-pygeoapi code example

```python
                generate_vigilance(layers.split(','),
                                   fh, mr, bbox.split(','), format_)
                out_file = open('vigilance.png', 'rb').read()
                return out_file
```
