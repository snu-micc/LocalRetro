import json
import pandas as pd

import torch
from torch import nn
import sklearn

from rdkit.Chem import PandasTools, AllChem

import dgl
from dgllife.utils import smiles_to_bigraph, WeaveAtomFeaturizer, CanonicalBondFeaturizer
from functools import partial

from scripts.utils import init_featurizer, load_model, collate_molgraphs_test
from scripts.get_edit import combined_edit
from LocalTemplate.template_decoder import get_idx_map, apply_template

def dearomatic(template):
    for s in ['[c;', '[o;', '[n;', '[s;', '[c@']:
        template = template.replace(s, s.upper())
    return template

def predict(model, graph, device):
    bg = dgl.batch([graph])
    bg.set_n_initializer(dgl.init.zero_initializer)
    bg.set_e_initializer(dgl.init.zero_initializer)
    bg = bg.to(device)
    node_feats = bg.ndata.pop('h').to(device)
    edge_feats = bg.edata.pop('e').to(device)
    return model(bg, node_feats, edge_feats)

def load_templates(args):
    atom_templates = pd.read_csv('%s/atom_templates.csv' % args['data_dir'])
    bond_templates = pd.read_csv('%s/bond_templates.csv' % args['data_dir'])
    smiles2smarts = pd.read_csv('%s/smiles2smarts.csv' % args['data_dir'])
    atom_templates = {atom_templates['Class'][i]: atom_templates['Template'][i] for i in atom_templates.index}
    bond_templates = {bond_templates['Class'][i]: bond_templates['Template'][i] for i in bond_templates.index}
    smarts2E = {smiles2smarts['Smarts_template'][i]: eval(smiles2smarts['edit_site'][i]) for i in smiles2smarts.index}
    smarts2H = {smiles2smarts['Smarts_template'][i]: eval(smiles2smarts['change_H'][i]) for i in smiles2smarts.index}
    return atom_templates, bond_templates, smarts2E, smarts2H

def init_LocalRetro(args):
    args = init_featurizer(args)
    model = load_model(args)
    atom_templates, bond_templates, smarts2E, smarts2H = load_templates(args)
    smiles_to_graph = partial(smiles_to_bigraph, add_self_loop=True)
    node_featurizer = WeaveAtomFeaturizer()
    edge_featurizer = CanonicalBondFeaturizer(self_loop=True)
    graph_function = lambda s: smiles_to_graph(s, node_featurizer = node_featurizer, edge_featurizer = edge_featurizer, canonical_atom_order = False)
    return model, graph_function, atom_templates, bond_templates, smarts2E, smarts2H


def remap(mol):
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx())
    
def retrosnythesis(smiles, model, graph_function, device, atom_templates, bond_templates, smarts2E, smarts2H, top_k = 10):
    model.eval()
    graph = graph_function(smiles)
    with torch.no_grad():
        atom_logits, bond_logits, _ = predict(model, graph, device)
        atom_logits = nn.Softmax(dim=1)(atom_logits)
        bond_logits = nn.Softmax(dim=1)(bond_logits)
        graph = graph.remove_self_loop()
        edit_id, edit_proba = combined_edit(graph, atom_logits, bond_logits, len(atom_templates), len(bond_templates), top_k)

    results = [(edit_id[k], edit_proba[k]) for k in range(top_k)]
    predicted_reactants = [smiles]
    predicted_edition = [None]
    predicted_scores = [None]
    idx_map = get_idx_map(smiles)
    for k, result in enumerate(results):
        edition = eval(str(result[0]))
        score = result[1]
        edit_idx = edition[0]
        template_class = edition[1]
        if type(edit_idx) == type(0):
            template = atom_templates[template_class]
            if len(template.split('>>')[0].split('.')) > 1:
                edit_idx = idx_map[edit_idx]
        else:
            template = bond_templates[template_class]
            if len(template.split('>>')[0].split('.')) > 1:
                edit_idx = (idx_map[edit_idx[0]], idx_map[edit_idx[1]])
        
        template_idx = smarts2E[template]
        H_change = smarts2H[template]
        predictions, fit_templates, matched_idx_list = apply_template(smiles, template, edit_idx, template_idx, H_change)
        if len(predictions) == 0:
                template = dearomatic(template)
                predictions, fit_templates, matched_idx_list = apply_template(smiles, template, edit_idx, template_idx, H_change)
                
        for reactant in predictions:
            if reactant not in predicted_reactants:
                predicted_reactants.append(reactant)
                predicted_edition.append('%s at %s' % (template, edit_idx))
                predicted_scores.append(score)
                
    results_df = pd.DataFrame({'SMILES': predicted_reactants, 'Local reaction template': predicted_edition, 'Score': predicted_scores})
    PandasTools.AddMoleculeColumnToFrame(results_df,'SMILES','Molecule')
    remap(results_df['Molecule'][0])
    return results_df
