import pandas as pd
import re
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
import matplotlib.pyplot as plt
import matplotlib
matplotlib.rcParams['font.sans-serif'] = ['SimHei']  # 或者 ['Microsoft YaHei']
matplotlib.rcParams['axes.unicode_minus'] = False

import seaborn as sns

# 第一步：读取 Excel 文件
files = ["C:\\Users\\22892\\Desktop\\分析文件\\P1（1-30）.xlsx",
    "C:\\Users\\22892\\Desktop\\分析文件\\P2（31-60）.xlsx",
    "C:\\Users\\22892\\Desktop\\分析文件\\P3（61-99）.xlsx"]
df_raw = pd.concat([pd.read_excel(f) for f in files], ignore_index=True)

# === 步骤2：清洗数据，提取“内容(翻译)”列 ===
df_cleaned = df_raw[df_raw['内容(翻译)'].notna()].copy()
df_cleaned.reset_index(drop=True, inplace=True)

def clean_text(text):
    text = str(text).lower()
    text = re.sub(r'<.*?>', '', text)                # 去除 HTML 标签
    text = re.sub(r'\d+', '', text)                  # 去除数字
    text = re.sub(r'\b\w{1,2}\b', '', text)          # 去除1~2字的单词（如 br、rv、33）
    text = re.sub(r'[^a-z ]+', '', text)             # 保留英文字母和空格
    return text

df_cleaned['cleaned'] = df_cleaned['内容(翻译)'].astype(str).apply(clean_text)

# === 步骤3：TF-IDF 向量化 ===
vectorizer = TfidfVectorizer(stop_words='english', max_features=100)
tfidf_matrix = vectorizer.fit_transform(df_cleaned['cleaned'])
feature_names = vectorizer.get_feature_names_out()

# === 步骤4：KMeans 聚类分析 ===
kmeans = KMeans(n_clusters=5, random_state=42, n_init='auto')
df_cleaned['cluster'] = kmeans.fit_predict(tfidf_matrix)

# === 步骤5：提取每个聚类中前10关键词 ===
cluster_keywords = {}
for i in range(5):
    indices = df_cleaned[df_cleaned['cluster'] == i].index.to_list()
    sub_matrix = tfidf_matrix[indices]
    mean_scores = sub_matrix.mean(axis=0).A1
    top_indices = mean_scores.argsort()[-10:][::-1]
    cluster_keywords[i] = [feature_names[idx] for idx in top_indices]

print("\n=== 每个聚类中的Top关键词 ===")
for cluster_id, keywords in cluster_keywords.items():
    print(f"Cluster {cluster_id}: {keywords}")

# === 步骤6：关键词分类：优点 vs 痛点 ===
positive_keywords = ['durable', 'sturdy', 'easy', 'perfect', 'great', 'cute', 'love', 'fit', 'strong', 'convenient']
negative_keywords = ['small', 'flimsy', 'broke', 'weak', 'hard', 'difficult', 'cheap', 'bad', 'wrong', 'disappointed']

keyword_stats = {kw: 0 for kw in positive_keywords + negative_keywords}
for text in df_cleaned['cleaned']:
    for kw in keyword_stats:
        if kw in text:
            keyword_stats[kw] += 1

# === 步骤7：整理为 DataFrame 方便可视化 ===
result_df = pd.DataFrame([
    {'keyword': k, 'count': v, 'type': '优点' if k in positive_keywords else '痛点'}
    for k, v in keyword_stats.items() if v > 0
])
result_df = result_df.sort_values(by='count', ascending=False)

# === 步骤8：可视化结果 ===
plt.figure(figsize=(12, 6))
sns.barplot(data=result_df, x='keyword', y='count', hue='type', palette='Set2')
plt.title('用户反馈关键词统计（优点 vs 痛点）')
plt.ylabel('出现次数')
plt.xlabel('关键词')
plt.xticks(rotation=45)
plt.tight_layout()
plt.show()

# === 步骤9：每条评论匹配优点/痛点关键词，并标记标签 ===
def classify_comment(text):
    text = text.lower()
    matched = []
    for kw in positive_keywords + negative_keywords:
        if kw in text:
            matched.append(kw)
    return matched

df_cleaned['matched_keywords'] = df_cleaned['cleaned'].apply(classify_comment)

# 标记优点/痛点标签
def label_type(matched_list):
    if not matched_list:
        return '无明显特征'
    elif any(kw in positive_keywords for kw in matched_list):
        return '优点'
    elif any(kw in negative_keywords for kw in matched_list):
        return '痛点'
    else:
        return '无明显特征'

df_cleaned['标签'] = df_cleaned['matched_keywords'].apply(label_type)

# 恢复原始内容列，整理输出
df_output = df_cleaned[['内容(翻译)', 'cluster', 'matched_keywords', '标签']].copy()
df_output.rename(columns={
    '内容(翻译)': '评论内容',
    'cluster': '聚类编号',
    'matched_keywords': '匹配关键词'
}, inplace=True)

# === 步骤10：输出到 Excel 或 CSV ===
df_output.to_excel('C:\\Users\\22892\\Desktop\\分析文件\\评论聚类分析结果.xlsx', index=False)
print("\n✅ 评论聚类分析已导出为：评论聚类分析结果.xlsx")
