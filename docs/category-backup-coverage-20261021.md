# 23類各池主1備2：真實快取盤點

🔴 **現有準備資料中，沒有任何一類的四池都具備3個已准入模型。** 23類共92池、目標276個位置；既有有限SQL研究只准入開源1、中1、高1、低0，因此研究准入不足273個位置。本盤點來源中的正式production資格仍是0，不能將公開排行榜的模型數當成可立即啟用的備援。

盤點時間：`2026-10-21T08:30:48.071301+00:00`。本次只讀既有快取，GET／POST／付費模型呼叫皆0；沒有讀取或使用F50答案。

🔵 **下表的「提案盤點」僅是未審核的原生分數與確切catalog模型身分對照數**，依各類自身門檻分池，不是已核准模型設定。順序一律為「開源／低／中／高」，每池目標3。同一模型的多個供應商不重複計數；同canonical身分跨池分數衝突時整個排除，不挑最高變體。開源盤點只表示該類有分數且catalog提供權重庫關聯，不代表授權、本機資源或開源API免費已確認。

| 類別 | 未審核提案盤點 開/低/中/高 | 研究已准入 開/低/中/高 | 距離3個仍缺 開/低/中/高 |
| --- | --- | --- | --- |
| `artificial-analysis:agentic_index` | 52/56/21/12 | 0/0/0/0 | 3/3/3/3 |
| `artificial-analysis:coding_index＊` | 65/35/47/36 | 1/0/1/1 | 2/3/2/2 |
| `artificial-analysis:intelligence_index` | 115/146/58/14 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:3d` | 45/60/42/4 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:asciiart` | 21/40/22/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:audiorealism` | 0/0/0/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:codecategories` | 52/61/54/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:dataviz` | 47/54/51/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:gamedev` | 53/65/49/2 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:graphicdesign` | 0/0/0/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:image` | 0/0/0/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:imageediting` | 0/0/0/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:logo` | 0/0/0/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:svg` | 35/54/24/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:text-to-speech` | 0/0/0/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:uicomponent` | 48/56/53/0 | 0/0/0/0 | 3/3/3/3 |
| `design-arena:models:website` | 55/65/55/0 | 0/0/0/0 | 3/3/3/3 |
| `openrouter:gpqa_diamond` | 87/22/66/61 | 0/0/0/0 | 3/3/3/3 |
| `openrouter:search_browsecomp` | 1/0/0/4 | 0/0/0/0 | 3/3/3/3 |
| `openrouter:search_dsqa` | 1/0/0/4 | 0/0/0/0 | 3/3/3/3 |
| `openrouter:search_hle` | 0/0/1/1 | 0/0/0/0 | 3/3/3/3 |
| `openrouter:search_widesearch` | 1/0/1/3 | 0/0/0/0 | 3/3/3/3 |
| `openrouter:tau_bench_verified_airline` | 83/29/113/1 | 0/0/0/0 | 3/3/3/3 |

＊coding列的3個准入僅適用既有`category50-sql-v8`固定版本／強度／供應商、有限SQL研究範圍，並非所有coding任務的正式資格。開源為Qwen3.8-27B、Near AI、medium/fp8；中池為GPT-OSS-120B、Near AI、high/fp4；高池為GLM-5.3-Flash、Wafer、low/nvfp4。低池原候選缺適用的獨立實測報告，既有準備檔已將它排除。

🟡 **實際缺少的資料會直接影響能否派送。** 其他22類沒有適用的已準備研究快照；公開對照仍缺來源變體／強度與實際供應商的明確綁定、各類獨立資格、完整流程成本與耗時證據。音訊、圖片等6類在這份catalog快取中沒有可確切對照且模態相符的模型，不能拿文字模型補數。所有類別仍缺新備援執行器對實際profile組合、可達故障與全域次數上限的完整流程預測／校準；既有單例QCS校準不能直接代用。

公開資料於台灣時間2026-10-21 06:44左右取得；主來源上游`as_of`為2026-10-20，搜尋4類上游`as_of`仍為2026-09-15，已超過各類48小時來源時效。搜尋列保留歷史盤點數供查核，其「仍在來源時效內」的提案數皆0。下載日期不能重設上游資料日期。既有SQL研究準備檔於台灣時間12:31建立、有效至隔日00:31；上述3個准入數是在本次盤點時間重新檢查所得，並非永久資格。

提案盤點尚未用完整成本／速度選定主1備2，也未套用完整費用資格。既有coding-only發現檔的20%價差只是固定token價格情境；快取、推理、分類、失敗、重試及後續升級成本缺少時，不能稱為完整降本或有利的Jev分類。沒有把該檔的全域coding池搬到其他22類。

下一步是逐類補明確模型版本／強度／供應商與對應資格，補足每池3個不同模型，再用新備援政策的完整流程資料核對成本、速度與可達備援路徑。未取得足夠資料的池繼續回報不足，不自動批准或換成未測模型。

來源、每個確切模型身分及所用來源列、排除原因、門檻、來源時效、雜湊與重放檢查均保存於[盤點證據](evidence/category-backup-coverage-20261021.json)。這是研究準備盤點，未部署、未推送、未改寫既有實驗結果。

## 92池主選與兩個備援的具名位置

🔵 **「研究准入」只指上述固定SQL研究；「待核」與「來源逾期」均是具名提案，不能派送。** 表中缺額保留「缺」，不能拿其他類別或其他品質池補位。所有位置都未取得新備援執行器的完整校準，production啟用數仍0。 276格中，3格為有限研究准入、132格是待核具名提案、15格是來源逾期的待核提案、126格明列缺額。

排序使用每個確切模型可比較的歷史報價：2000個未快取輸入token＋1000個輸出token；先按USD報價，再按端點歷史p50毫秒（缺值排最後）、模型ID與供應商排序。已有研究准入設定先列第一格，之後的待核備援優先不同供應商，再按同一報價排序。這是整理提案的方法，沒有把token報價稱為完整成本、沒有把端點p50稱為整題耗時，也沒有推測未明示的模型家族。供應商不同不證明實體故障範圍完全獨立，所有OpenRouter端點仍共享入口。

每格顯示「請求model ID／供應商／狀態」。來源未明示的實際版本或強度在JSON保留null；不能因名稱看似相同便假定變體已對照。`:free`路徑保留原樣，不改成付費canonical。

| 類別 | 池 | 主選位置 | 備援1位置 | 備援2位置 |
| --- | --- | --- | --- | --- |
| `artificial-analysis:agentic_index` | 開源 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `cohere/north-mini-code:free`／`cohere`／待核 | `google/gemma-4-26b-a4b-it:free`／`google-ai-studio`／待核 |
| `artificial-analysis:agentic_index` | 低 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `cohere/north-mini-code:free`／`cohere`／待核 | `google/gemma-4-26b-a4b-it:free`／`google-ai-studio`／待核 |
| `artificial-analysis:agentic_index` | 中 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 | `inclusionai/ling-3.0-flash-vl`／`deepinfra/fp16`／待核 | `deepseek/deepseek-v4-flash`／`streamlake/fp8`／待核 |
| `artificial-analysis:agentic_index` | 高 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `z-ai/glm-5.3`／`wafer/us`／待核 | `meta/muse-spark-1.3`／`meta`／待核 |
| `artificial-analysis:coding_index` | 開源 | `qwen/qwen3.8-27b`／`near-ai/fp8`／研究准入＊ | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `liquid/lfm-2.5-2.6b:free`／`liquid/fp8`／待核 |
| `artificial-analysis:coding_index` | 低 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `liquid/lfm-2.5-2.6b:free`／`liquid/fp8`／待核 | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`／`nvidia`／待核 |
| `artificial-analysis:coding_index` | 中 | `openai/gpt-oss-120b`／`near-ai/fp4`／研究准入＊ | `cohere/north-mini-code:free`／`cohere`／待核 | `google/gemma-4-26b-a4b-it:free`／`google-ai-studio`／待核 |
| `artificial-analysis:coding_index` | 高 | `z-ai/glm-5.3-flash`／`wafer/nvfp4`／研究准入＊ | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 | `deepseek/deepseek-v4-pro-0813`／`baidu/fp8`／待核 |
| `artificial-analysis:intelligence_index` | 開源 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `liquid/lfm-2.5-2.6b:free`／`liquid/fp8`／待核 | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`／`nvidia`／待核 |
| `artificial-analysis:intelligence_index` | 低 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `liquid/lfm-2.5-2.6b:free`／`liquid/fp8`／待核 | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`／`nvidia`／待核 |
| `artificial-analysis:intelligence_index` | 中 | `thinkingmachines/inkling-small:free`／`thinkingmachines/nvfp4`／待核 | `minimax/minimax-m3.1-flash:free`／`minimax`／待核 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 |
| `artificial-analysis:intelligence_index` | 高 | `xiaomi/mimo-v2.6-pro`／`deepinfra/fp8`／待核 | `meta/muse-spark-1.3`／`meta`／待核 | `openai/gpt-6-sol`／`openai/flex`／待核 |
| `design-arena:models:3d` | 開源 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 |
| `design-arena:models:3d` | 低 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 |
| `design-arena:models:3d` | 中 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 | `deepseek/deepseek-v4.1-flash-1017`／`decart/fp4`／待核 |
| `design-arena:models:3d` | 高 | `openai/gpt-6.1-sol`／`openai/flex`／待核 | `anthropic/claude-sonnet-5.5`／`claude-on-aws`／待核 | `anthropic/claude-opus-5.5`／`azure/global`／待核 |
| `design-arena:models:asciiart` | 開源 | `nvidia/nemotron-3-ultra-550b-a55b:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 |
| `design-arena:models:asciiart` | 低 | `nvidia/nemotron-3-ultra-550b-a55b:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 |
| `design-arena:models:asciiart` | 中 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `z-ai/glm-5.3`／`wafer/us`／待核 | `google/gemini-3.6-flash`／`google-ai-studio/flex`／待核 |
| `design-arena:models:asciiart` | 高 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `design-arena:models:audiorealism` | 開源 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:audiorealism` | 低 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:audiorealism` | 中 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:audiorealism` | 高 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:codecategories` | 開源 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 |
| `design-arena:models:codecategories` | 低 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 | `openai/gpt-5-nano`／`openai/flex`／待核 |
| `design-arena:models:codecategories` | 中 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 |
| `design-arena:models:codecategories` | 高 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `design-arena:models:dataviz` | 開源 | `nvidia/nemotron-3-ultra-550b-a55b:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-20b`／`darkbloom/fp8`／待核 |
| `design-arena:models:dataviz` | 低 | `nvidia/nemotron-3-ultra-550b-a55b:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-20b`／`darkbloom/fp8`／待核 |
| `design-arena:models:dataviz` | 中 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `xiaomi/mimo-v2.5`／`gmicloud/fp8`／待核 | `anthropic/claude-haiku-5.5`／`claude-on-aws`／待核 |
| `design-arena:models:dataviz` | 高 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `design-arena:models:gamedev` | 開源 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 |
| `design-arena:models:gamedev` | 低 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 |
| `design-arena:models:gamedev` | 中 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 | `deepseek/deepseek-v4.1-flash-1017`／`decart/fp4`／待核 |
| `design-arena:models:gamedev` | 高 | `anthropic/claude-sonnet-5.5`／`claude-on-aws`／待核 | `anthropic/claude-opus-5.5`／`azure/global`／待核 | 缺：該類此池不同模型不足 |
| `design-arena:models:graphicdesign` | 開源 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:graphicdesign` | 低 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:graphicdesign` | 中 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:graphicdesign` | 高 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:image` | 開源 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:image` | 低 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:image` | 中 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:image` | 高 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:imageediting` | 開源 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:imageediting` | 低 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:imageediting` | 中 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:imageediting` | 高 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:logo` | 開源 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:logo` | 低 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:logo` | 中 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:logo` | 高 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:svg` | 開源 | `nvidia/nemotron-3-ultra-550b-a55b:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 |
| `design-arena:models:svg` | 低 | `nvidia/nemotron-3-ultra-550b-a55b:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 |
| `design-arena:models:svg` | 中 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `z-ai/glm-5.3`／`wafer/us`／待核 | `qwen/qwen3.7-plus`／`alibaba`／待核 |
| `design-arena:models:svg` | 高 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `design-arena:models:text-to-speech` | 開源 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:text-to-speech` | 低 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:text-to-speech` | 中 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:text-to-speech` | 高 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 | 缺：來源版本／模態無可用catalog對照 |
| `design-arena:models:uicomponent` | 開源 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 |
| `design-arena:models:uicomponent` | 低 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 | `openai/gpt-5-nano`／`openai/flex`／待核 |
| `design-arena:models:uicomponent` | 中 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 |
| `design-arena:models:uicomponent` | 高 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `design-arena:models:website` | 開源 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `openai/gpt-oss-20b`／`darkbloom/fp8`／待核 |
| `design-arena:models:website` | 低 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `openai/gpt-oss-20b`／`darkbloom/fp8`／待核 | `openai/gpt-oss-120b`／`near-ai/fp4`／待核 |
| `design-arena:models:website` | 中 | `thinkingmachines/inkling:free`／`thinkingmachines/nvfp4`／待核 | `z-ai/glm-5.3-flash`／`open-inference/fp4`／待核 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核 |
| `design-arena:models:website` | 高 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `openrouter:gpqa_diamond` | 開源 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `google/gemma-4-26b-a4b-it:free`／`google-ai-studio`／待核 | `thinkingmachines/inkling-small:free`／`thinkingmachines/nvfp4`／待核 |
| `openrouter:gpqa_diamond` | 低 | `meta-llama/llama-3.1-8b-instruct`／`deepinfra/fp8`／待核 | `sao10k/l3-lunaris-8b`／`deepinfra/turbo`／待核 | `google/gemma-3-4b-it`／`parasail/bf16`／待核 |
| `openrouter:gpqa_diamond` | 中 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `google/gemma-4-26b-a4b-it:free`／`google-ai-studio`／待核 | `nvidia/nemotron-3-ultra-550b-a55b:free`／`nvidia`／待核 |
| `openrouter:gpqa_diamond` | 高 | `thinkingmachines/inkling-small:free`／`thinkingmachines/nvfp4`／待核 | `nvidia/nemotron-3.5-super-vl:free`／`nvidia`／待核 | `deepseek/deepseek-v4.1-flash`／`open-inference/fp4`／待核 |
| `openrouter:search_browsecomp` | 開源 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核・來源逾期 | 缺：該類此池不同模型不足 | 缺：該類此池不同模型不足 |
| `openrouter:search_browsecomp` | 低 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `openrouter:search_browsecomp` | 中 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `openrouter:search_browsecomp` | 高 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核・來源逾期 | `openai/gpt-5.6-luna`／`openai/flex`／待核・來源逾期 | `anthropic/claude-opus-5`／`azure/global`／待核・來源逾期 |
| `openrouter:search_dsqa` | 開源 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核・來源逾期 | 缺：該類此池不同模型不足 | 缺：該類此池不同模型不足 |
| `openrouter:search_dsqa` | 低 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `openrouter:search_dsqa` | 中 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `openrouter:search_dsqa` | 高 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核・來源逾期 | `openai/gpt-5.6-luna`／`openai/flex`／待核・來源逾期 | `anthropic/claude-opus-5`／`azure/global`／待核・來源逾期 |
| `openrouter:search_hle` | 開源 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `openrouter:search_hle` | 低 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `openrouter:search_hle` | 中 | `anthropic/claude-opus-5`／`azure/global`／待核・來源逾期 | 缺：該類此池不同模型不足 | 缺：該類此池不同模型不足 |
| `openrouter:search_hle` | 高 | `openai/gpt-5.6-sol`／`openai/flex`／待核・來源逾期 | 缺：該類此池不同模型不足 | 缺：該類此池不同模型不足 |
| `openrouter:search_widesearch` | 開源 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核・來源逾期 | 缺：該類此池不同模型不足 | 缺：該類此池不同模型不足 |
| `openrouter:search_widesearch` | 低 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 | 缺：該類原生門檻內沒有模型 |
| `openrouter:search_widesearch` | 中 | `anthropic/claude-opus-5`／`azure/global`／待核・來源逾期 | 缺：該類此池不同模型不足 | 缺：該類此池不同模型不足 |
| `openrouter:search_widesearch` | 高 | `deepseek/deepseek-v4-flash-0731`／`relace/fp4`／待核・來源逾期 | `openai/gpt-5.6-luna`／`openai/flex`／待核・來源逾期 | `openai/gpt-5.6-sol`／`openai/flex`／待核・來源逾期 |
| `openrouter:tau_bench_verified_airline` | 開源 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `google/gemma-4-26b-a4b-it:free`／`google-ai-studio`／待核 | `thinkingmachines/inkling-small:free`／`thinkingmachines/nvfp4`／待核 |
| `openrouter:tau_bench_verified_airline` | 低 | `meta-llama/llama-3.1-8b-instruct`／`deepinfra/fp8`／待核 | `google/gemma-3-12b-it`／`deepinfra/bf16/flex`／待核 | `openai/gpt-5-nano`／`openai/flex`／待核 |
| `openrouter:tau_bench_verified_airline` | 中 | `nvidia/nemotron-3.5-lightning:free`／`nvidia/nvfp4`／待核 | `google/gemma-4-26b-a4b-it:free`／`google-ai-studio`／待核 | `thinkingmachines/inkling-small:free`／`thinkingmachines/nvfp4`／待核 |
| `openrouter:tau_bench_verified_airline` | 高 | `anthropic/claude-fable-5`／`azure`／待核 | 缺：該類此池不同模型不足 | 缺：該類此池不同模型不足 |

＊研究准入3個仍只有原SQL範圍；待核位置不增加前表的3個研究准入總數。各位置的精確報價、已知供應商、量化、來源列、缺少的版本／強度，以及沒有可比較報價的發現清單均在JSON的`named_pool_positions.pools`。

## 無法填具名位置的來源發現

這些來源名稱確實出現在既有觀測，但沒有可用的精確catalog版本／模態對照，因此只列在發現清單，不能放進三個候選位置：

- `design-arena:models:audiorealism`：`fish-audio/drama-3-20261013`、`fish-audio/s2.1-pro-20260729`、`google/gemini-3.8-flash-tts-20260922`、`microsoft/mai-voice-2`、`microsoft/mai-voice-2-flash-20260723`、`qwen/qwen-audio-3.0-tts-flash-20260723`。
- `design-arena:models:graphicdesign`：`black-forest-labs/flux.2-flex`、`black-forest-labs/flux.2-klein-4b`、`black-forest-labs/flux.2-pro`、`google/gemini-2.5-flash-image`、`google/gemini-3-pro-image-preview-20251120`、`google/gemini-3.1-flash-image-preview-20260226`、`microsoft/mai-image-2.5`、`microsoft/mai-image-2.6-20260904`、`microsoft/mai-image-2.6-flash-20260904`、`openai/gpt-5-image`、`openai/gpt-5-image-mini`、`sourceful/riverflow-v2.5-pro-20260605`。
- `design-arena:models:image`：`black-forest-labs/flux.2-flex`、`black-forest-labs/flux.2-klein-4b`、`black-forest-labs/flux.2-pro`、`google/gemini-2.5-flash-image`、`google/gemini-3-pro-image-preview-20251120`、`google/gemini-3.1-flash-image-preview-20260226`、`microsoft/mai-image-2.5`、`microsoft/mai-image-2.6-20260904`、`microsoft/mai-image-2.6-flash-20260904`、`openai/gpt-5-image`、`openai/gpt-5-image-mini`、`sourceful/riverflow-v2.5-pro-20260605`。
- `design-arena:models:imageediting`：`black-forest-labs/flux.2-klein-4b`、`black-forest-labs/flux.2-pro`、`google/gemini-3-pro-image-preview-20251120`、`microsoft/mai-image-2.5`、`microsoft/mai-image-2.6-20260904`、`microsoft/mai-image-2.6-flash-20260904`、`openai/gpt-image-1`、`sourceful/riverflow-v2.5-pro-20260605`。
- `design-arena:models:logo`：`black-forest-labs/flux.2-flex`、`black-forest-labs/flux.2-klein-4b`、`black-forest-labs/flux.2-pro`、`google/gemini-2.5-flash-image`、`google/gemini-3-pro-image-preview-20251120`、`google/gemini-3.1-flash-image-preview-20260226`、`microsoft/mai-image-2.5`、`microsoft/mai-image-2.6-20260904`、`microsoft/mai-image-2.6-flash-20260904`、`openai/gpt-5-image`、`openai/gpt-5-image-mini`、`sourceful/riverflow-v2.5-pro-20260605`。
- `design-arena:models:text-to-speech`：`google/gemini-3.8-flash-tts-20260922`、`microsoft/mai-voice-2`、`microsoft/mai-voice-2-flash-20260723`。

其他高池空缺是該類現有原生門檻內沒有候選，或不同模型不足3個；不把中池移到高池。完整發現與排除清單在同一證據檔。

## SQL低池既有3個待核配置

下列是現有`examples/sql-low-pool-candidates-v1.json`的指定順序，和上述歷史盤點的報價排序分開保留；本次沒有重排或啟用原配置。3個皆`approved:false`，SQL實測、完整成本／耗時與傳輸接入仍待完成，所以低池實際准入仍0。

| 原配置位置 | 請求模型 | 強度／供應商／量化 | 狀態 |
| --- | --- | --- | --- |
| 主選 | `meta-llama/llama-3.1-8b-instruct` | `none`／`deepinfra/fp8`／`fp8` | 待核、不可派送 |
| 備援1 | `google/gemma-3-4b-it` | `none`／`parasail/bf16`／`bf16` | 待核、不可派送 |
| 備援2 | `nvidia/nemotron-3.5-lightning:free` | `provider_default`／`nvidia/nvfp4`／`nvfp4` | 待核、不可派送 |

Llama／Gemma的原配置要求省略reasoning參數；Lightning的`:free`請求不能改成付費模型。20%只限來源檔列明的token情境，Gemma在長輸入短輸出情境未達門檻；不將該情境算作完整流程降本。
