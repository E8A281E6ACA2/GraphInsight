"""
Schema 提取服务
自动提取 Neo4j 图谱的 Schema 信息
"""
from typing import Dict, List
from services.neo4j_service import Neo4jService


class SchemaService:
    """提取 Neo4j 图谱的 Schema 信息"""
    
    def __init__(self):
        self.neo4j_service = Neo4jService()
    
    def get_schema(self) -> Dict:
        """
        获取完整的图谱 Schema
        
        Returns:
            包含节点类型、关系类型和属性的字典
        """
        return {
            "node_types": self.get_node_types(),
            "relationship_types": self.get_relationship_types(),
            "node_properties": self.get_node_properties(),
            "relationship_properties": self.get_relationship_properties()
        }
    
    def get_node_types(self) -> List[str]:
        """
        获取所有节点类型（标签）
        
        Returns:
            节点类型列表
        """
        query = "CALL db.labels()"
        result = self.neo4j_service.execute_query(query)
        
        labels = []
        for record in result.get("records", []):
            if "label" in record:
                labels.append(record["label"])
        
        return labels
    
    def get_relationship_types(self) -> List[str]:
        """
        获取所有关系类型
        
        Returns:
            关系类型列表
        """
        query = "CALL db.relationshipTypes()"
        result = self.neo4j_service.execute_query(query)
        
        types = []
        for record in result.get("records", []):
            if "relationshipType" in record:
                types.append(record["relationshipType"])
        
        return types
    
    def get_node_properties(self) -> Dict[str, List[str]]:
        """
        获取每种节点类型的属性
        
        Returns:
            节点类型到属性列表的映射
        """
        node_types = self.get_node_types()
        properties = {}
        
        for node_type in node_types:
            query = f"""
            MATCH (n:{node_type})
            WITH n LIMIT 10
            UNWIND keys(n) as key
            RETURN DISTINCT key
            """
            result = self.neo4j_service.execute_query(query)
            
            props = []
            for record in result.get("records", []):
                if "key" in record:
                    props.append(record["key"])
            
            properties[node_type] = props
        
        return properties
    
    def get_relationship_properties(self) -> Dict[str, List[str]]:
        """
        获取每种关系类型的属性
        
        Returns:
            关系类型到属性列表的映射
        """
        rel_types = self.get_relationship_types()
        properties = {}
        
        for rel_type in rel_types:
            query = f"""
            MATCH ()-[r:{rel_type}]->()
            WITH r LIMIT 10
            UNWIND keys(r) as key
            RETURN DISTINCT key
            """
            result = self.neo4j_service.execute_query(query)
            
            props = []
            for record in result.get("records", []):
                if "key" in record:
                    props.append(record["key"])
            
            properties[rel_type] = props
        
        return properties
    
    def get_schema_summary(self) -> str:
        """
        获取 Schema 的文本摘要，用于 Prompt
        
        Returns:
            Schema 摘要文本
        """
        schema = self.get_schema()
        
        summary = "知识图谱 Schema：\n\n"
        
        # 节点类型
        summary += "节点类型：\n"
        for node_type in schema["node_types"]:
            props = schema["node_properties"].get(node_type, [])
            summary += f"  - {node_type}"
            if props:
                summary += f" (属性: {', '.join(props[:5])})"  # 只显示前5个属性
            summary += "\n"
        
        # 关系类型
        summary += "\n关系类型：\n"
        for rel_type in schema["relationship_types"]:
            props = schema["relationship_properties"].get(rel_type, [])
            summary += f"  - {rel_type}"
            if props:
                summary += f" (属性: {', '.join(props[:3])})"  # 只显示前3个属性
            summary += "\n"
        
        return summary
