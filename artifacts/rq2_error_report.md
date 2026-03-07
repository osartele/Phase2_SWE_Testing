# RQ2 Error Report

Generated at: `2026-03-07T01:19:58Z`

## Metrics Summary

- Records evaluated: `833`
- Signature records: `833`
- Top-1 accuracy (identifier): `0.9916`
- Top-3 accuracy (identifier): `0.9928`
- Top-1 accuracy (signature): `0.9496`
- Top-3 accuracy (signature): `0.9880`
- Top-1 accuracy (primary): `0.9496`
- Top-3 accuracy (primary): `0.9880`

## Failure Modes (Top-1 Primary Misses)

### overloads (28)

- `c2t_06136c7b80736ce28570` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_0b6cd176e49c7aad4de1` repo `42949039` labeled `evaluate` top1 `evaluate` (rank=1)
- `c2t_0c752fdfed1db707360a` repo `42949039` labeled `getProperty` top1 `getProperty` (rank=1)
- `c2t_0d6ce629860bb7012a7c` repo `42949039` labeled `getBusinessObjectDataUploadCredential` top1 `getBusinessObjectDataUploadCredential` (rank=1)
- `c2t_14cf6f680e383639df8b` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_17c55d8dfa26e8f117b5` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_2255d4c6e6455faa6598` repo `42949039` labeled `evaluate` top1 `evaluate` (rank=1)
- `c2t_249aee45bc8dc9736dd1` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_34eb2ac3574aaa1580d2` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_35aa830e11eec4efff84` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_4bb5327013e2423beeb1` repo `42949039` labeled `getProperty` top1 `getProperty` (rank=1)
- `c2t_4fc815c36588b8118387` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_505d3023cf0c2ef61d6e` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_5dabea0a872e4ca5e622` repo `42949039` labeled `getProperty` top1 `getProperty` (rank=1)
- `c2t_5dd68abcf5ac085f5238` repo `42949039` labeled `getProperty` top1 `getProperty` (rank=1)
- `c2t_791e868fb6b9e182395b` repo `42949039` labeled `getProperty` top1 `getProperty` (rank=1)
- `c2t_7e6dfc1deaf0c64ea571` repo `42949039` labeled `getBusinessObjectDataUploadCredential` top1 `getBusinessObjectDataUploadCredential` (rank=1)
- `c2t_80a619292653ddff71dd` repo `42949039` labeled `evaluate` top1 `evaluate` (rank=1)
- `c2t_8129c875725133e500fd` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- `c2t_8baa094a533d13f08942` repo `42949039` labeled `getStorageAttributeIntegerValueByName` top1 `getStorageAttributeIntegerValueByName` (rank=1)
- ... 8 more

### mocks (8)

- `c2t_28c686419a15a54ae5d4` repo `42949039` labeled `getStorageFileEntity` top1 `getStorageFileEntity` (rank=1)
- `c2t_2bf24fcdbae4a39065b7` repo `42949039` labeled `getStorageFileEntity` top1 `getStorageFileEntity` (rank=1)
- `c2t_76c82a0a594e6c55eae2` repo `42949039` labeled `getStorageFileEntity` top1 `getStorageFileEntity` (rank=1)
- `c2t_a860579ae4bf043c4cd7` repo `105200994` labeled `getRecursiveFiles` top1 `getRecursiveFiles` (rank=1)
- `c2t_a882de2d0a2f84f88f95` repo `4159017` labeled `format` top1 `toString` (rank=2)
- `c2t_a895dbbd253605cac3e8` repo `42949039` labeled `getStorageFileEntity` top1 `getStorageFileEntity` (rank=1)
- `c2t_ae5885f36016cb8789be` repo `42949039` labeled `createStorageFileEntitiesFromStorageFiles` top1 `createStorageFileEntitiesFromStorageFiles` (rank=1)
- `c2t_e87c9da3fd7e060efb39` repo `42949039` labeled `getBusinessObjectDefinitions` top1 `getBusinessObjectDefinitions` (rank=1)

### other (5)

- `c2t_247317467eac2252d72b` repo `40266095` labeled `PageRank` top1 `nodeRank` (rank=None)
- `c2t_92ee658d2f04375c33d4` repo `42949039` labeled `onStartup` top1 `initServletMapping` (rank=8)
- `c2t_b7c74b867a674820b3d7` repo `4159017` labeled `absent` top1 `isPresent` (rank=16)
- `c2t_c4e0d05e7540cd0d4e31` repo `28992330` labeled `ApacheHttpTransport` top1 `isMtls` (rank=None)
- `c2t_ff68e58e5bff4d075ac5` repo `4159017` labeled `Input` top1 `input` (rank=None)

### processing_error (1)

- `c2t_a0368c03cdc3830d9390` repo `109219866` labeled `getDefault` top1 `` (rank=)
